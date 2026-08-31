"""Request coordinator: central orchestration boundary for proxy lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import typing
import zlib
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import httpx

from eggpool.accounts.registry import AccountRegistry, AccountRuntimeIdentity
from eggpool.catalog.protocols import ProtocolMismatchError
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
    AcceptedFinalizationInvariantError,
    AggregatorError,
    AuthenticationError,
    CapabilityError,
    DatabaseError,
    ModelUnavailableError,
    PrematureStreamEOFError,
    RequestTooLargeError,
    UpstreamError,
    UpstreamExhaustedError,
)
from eggpool.failure import (
    EffectsApplier,
    FailureEffectProgress,
    FailureEffects,
    FailureObservation,
    ModelQuarantine,
)
from eggpool.failure.classifier import classify_failure_effects
from eggpool.health.backoff import compute_backoff_seconds
from eggpool.health.health_manager import (
    FailureCategory,
    classify_failure_category,
)
from eggpool.jsonx import dumps_str as jsonx_dumps_str
from eggpool.jsonx import loads as jsonx_loads
from eggpool.metrics.thinking import get_counter
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.contract import (
    build_auth_headers,
    build_static_headers,
    build_upstream_headers,
)
from eggpool.proxy.client import filter_response_headers
from eggpool.proxy.normalized_usage import (
    CacheCounterStatus,
    NormalizedUsage,
    UsageParseDiag,
    emit_parse_failure_log,
    normalize_from_stream_result,
    normalize_usage,
)
from eggpool.proxy.sse import SSEDecoder
from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.attempt_finalizer import (
    AttemptFinalizationData,
    AttemptFinalizer,
)
from eggpool.request.body import encode_json_body
from eggpool.request.finalization_job import (
    AttemptRuntimeLease,
    ClaimCompensationProgress,
    ClaimCompensationSubmission,
    FailedAttemptCleanupProgress,
    FailedAttemptCleanupSubmission,
    FinalizationCapacityError,
    FinalizationIdentity,
    RuntimePublicationReceipt,
    TerminalCommandProgress,
)
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)
from eggpool.request.limits import estimate_reservation_tokens
from eggpool.request.parsed_payload import ParsedRequestPayload  # noqa: TC001
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.request.response_handoff import ResponseHandoffState
from eggpool.request.selection_claim_diagnostics import (
    SelectionClaimDiagnostics,
    get_selection_claim_diagnostics,
)
from eggpool.request.stream_completion import (
    CompletionPolicy,
    StreamEOFDecision,
    classify_stream_eof,
)
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    STREAM_OUTCOME_COMPLETED_CANONICAL,
    STREAM_OUTCOME_COMPLETED_COMPATIBILITY,
    STREAM_OUTCOME_EMPTY_EOF,
    STREAM_OUTCOME_FIRST_BYTE_TIMEOUT,
    STREAM_OUTCOME_IDLE_TIMEOUT,
    STREAM_OUTCOME_MALFORMED_EOF,
    STREAM_OUTCOME_PREMATURE_EOF_BEFORE_BODY,
    STREAM_OUTCOME_PREMATURE_EOF_MIDSTREAM,
    STREAM_OUTCOME_RESPONSE_HEADER_TIMEOUT,
    STREAM_OUTCOME_TERMINAL_FAILURE,
    STREAM_OUTCOME_TERMINAL_INCOMPLETE,
    STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    ProviderStreamTimeoutError,
    StreamDiagnostics,
    classify_httpx_error_class,
    get_stream_diagnostics,
)
from eggpool.retry.classification import RetryCategory, RetryClassifier
from eggpool.routing.router import RoutingDecisionTrace, RoutingExclusion
from eggpool.runtime_dispatch import (
    SPAN_ACCOUNT_LOOKUP,
    SPAN_CIRCUIT_PROBE,
    SPAN_CLAIM_ROLLBACK,
    SPAN_DB_WRITE_ATTEMPT,
    SPAN_DB_WRITE_REQUEST,
    SPAN_DB_WRITE_RESERVATION,
    SPAN_DISPATCH_PERSISTENCE_COMMIT,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_DISPATCH_PERSISTENCE_TRANSACTION,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_POST_COMMIT_COMPENSATION,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_POST_COMMIT_PUBLICATION,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_RESERVATION_ESTIMATE,
    SPAN_ROUTING_PLAN,
    SPAN_ROUTING_TRACE_BUILD,
    SPAN_ROUTING_TRACE_WRITE,
    SPAN_RUNTIME_PUBLICATION,
    SPAN_SELECTION_CLAIM_HELD,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_SELECTION_CLAIM_WAIT,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_SELECTION_REVALIDATION,  # type: ignore[reportUnusedImport]  # noqa: F401
    SPAN_THINKING_CLASSIFICATION,
    DispatchSpanRecorder,
    DispatchSpanTimer,
)
from eggpool.security.redaction import redact_error_detail
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.protocol import BodyTranscoder, select_transcoder
from eggpool.transcoder.streaming import select_streaming_transcoder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from eggpool.catalog.capabilities import (
        ThinkingCapability,
        ThinkingRequestRequirement,
    )
    from eggpool.catalog.pricing import CostCalculator
    from eggpool.catalog.service import CatalogService
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.proxy.usage import StreamUsageResult
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router
    from eggpool.transcoder.policy import TranscoderPolicy
    from eggpool.transcoder.prepared import PreparedTranscode

logger = logging.getLogger(__name__)


def _redact_auth_shape(auth_headers: dict[str, str]) -> str:
    """Return a redacted representation of auth headers for debug logging."""
    if not auth_headers:
        return "none"
    parts: list[str] = []
    for name, value in auth_headers.items():
        parts.append(f"{name}: present length={len(value)}")
    return ", ".join(parts)


# Default maximum retry attempts for pre-body failures
DEFAULT_MAX_RETRY_ATTEMPTS = 3

_ATTEMPT_SELECTION_METADATA_KEYS = (
    "_post_commit_selected",
    "post_commit_published",
    "post_commit_interrupted",
)

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
_SUCCESS_CLEAR_BACKOFF_REASONS: tuple[str, ...] = (
    *_TRANSIENT_BACKOFF_REASONS,
    "model_unavailable",
)


def _prepare_error_detail(value: object | None, persist: bool) -> str | None:
    """Redact error detail only when persistence is enabled."""
    if not persist or value is None:
        return None
    return redact_error_detail(str(value))


def _serialize_thinking_trace(trace: dict[str, Any] | None) -> str | None:
    """Serialize thinking trace to JSON for persistence."""
    return jsonx_dumps_str(trace) if trace else None


@contextlib.contextmanager
def _maybe_span(
    recorder: DispatchSpanRecorder | None,
    name: str,
) -> typing.Generator[None, None, None]:
    """Record a named span only when ``recorder`` is present.

    Falls back to a no-op context manager when the recorder is missing
    so callers can wrap suspect regions unconditionally without
    measuring span scaffolding cost.  Errors inside the body are
    propagated so the lock / transaction semantics are unaffected.
    """
    if recorder is None:
        yield
        return
    timer: DispatchSpanTimer = recorder.measure(name)
    with timer:
        yield


def resolve_selected_provider_kind(
    catalog: Any,  # noqa: ANN401
    selected: Any,  # noqa: ANN401
    config: Any = None,  # noqa: ANN401
) -> str | None:
    """Look up the selected provider's ``kind``.

    Lookup order:

    1. ``catalog.providers[provider_id].kind`` (catalog-backed metadata)
    2. ``config.providers[provider_id].kind`` (config-backed metadata
       when the catalog row is missing or has no ``kind``)

    Returns ``None`` when neither source carries a ``kind`` or when
    the selected attempt has no ``provider_id``.  Never raises.
    """
    if not selected or not getattr(selected, "provider_id", None):
        return None
    provider_id: str = selected.provider_id
    try:
        providers_obj: Any = getattr(catalog, "providers", None)
        if isinstance(providers_obj, dict):
            providers_dict: dict[str, Any] = cast("dict[str, Any]", providers_obj)
            provider_config: Any = providers_dict.get(provider_id)
            if provider_config is not None:
                kind_attr: Any = getattr(provider_config, "kind", None)
                if isinstance(kind_attr, str) and kind_attr:
                    return kind_attr
    except Exception:  # noqa: BLE001
        pass
    try:
        if config is not None:
            config_providers: Any = getattr(config, "providers", None)
            if isinstance(config_providers, dict):
                config_dict: dict[str, Any] = cast("dict[str, Any]", config_providers)
                provider_config: Any = config_dict.get(provider_id)
                if provider_config is not None:
                    kind_attr: Any = getattr(provider_config, "kind", None)
                    if isinstance(kind_attr, str) and kind_attr:
                        return kind_attr
    except Exception:  # noqa: BLE001
        return None
    return None


def _build_normalized_usage(
    *,
    usage: StreamUsageResult | None,
    raw_payload: Any | None,
    protocol: str,
    provider_id: str | None,
    model_id: str | None,
    is_streaming: bool,
) -> NormalizedUsage | None:
    """Build a :class:`NormalizedUsage` for a completed request.

    The non-streaming path passes the full upstream JSON body so the
    helper can re-extract cache fields with the same key-presence
    semantics as :func:`normalize_usage`.  The streaming path passes
    only the merged :class:`StreamUsageResult` because the raw
    payload is not preserved across SSE frames; in that case the
    helper calls :func:`normalize_from_stream_result` so the
    zero-vs-``None`` distinction is preserved from the per-event
    counters.

    Returns ``None`` when both inputs are unavailable so the
    finalizer can fall back to the legacy zero-token columns.

    Emits structured diagnostic logs via
    :func:`eggpool.proxy.normalized_usage.emit_parse_failure_log`
    for every parse-failure path:

    * ``unknown_shape`` — ``raw_payload`` present but
      ``normalize_usage`` returned ``UNKNOWN_FORMAT`` with a
      parseable ``raw_usage`` block (shape unrecognized).
    * ``preserved_raw_only`` — ``raw_payload`` present but
      ``normalize_usage`` returned ``UNKNOWN_FORMAT`` with no
      ``raw_usage`` block (payload not parseable).
    * ``missing_final_stream_event`` — streaming ended before the
      final usage-bearing SSE event arrived.
    * ``missing_usage_block`` — both ``raw_payload`` and ``usage``
      are ``None``.
    """
    if raw_payload is not None:
        normalized = normalize_usage(raw_payload, protocol=protocol)
        if normalized.cache_counter_status == CacheCounterStatus.UNKNOWN_FORMAT:
            # The upstream payload was present but EggPool could not
            # classify the usage shape.  Emit the appropriate diagnostic:
            # ``cache_usage_unknown_shape`` when a usage block existed
            # but its shape was unrecognized (raw_usage is populated);
            # ``usage_parse_preserved_raw_only`` when the payload could
            # not be parsed at all (no usable usage block found).
            if normalized.raw_usage is not None:
                emit_parse_failure_log(
                    _diag_for(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        reason="unknown_shape",
                    )
                )
            else:
                emit_parse_failure_log(
                    _diag_for(
                        provider_id=provider_id,
                        model_id=model_id,
                        protocol=protocol,
                        reason="preserved_raw_only",
                    )
                )
        return normalized

    if usage is not None:
        normalized = normalize_from_stream_result(
            usage,
            protocol=protocol,
            raw_usage=None,
        )
        if (
            is_streaming
            and normalized.cache_counter_status != CacheCounterStatus.REPORTED
            and not getattr(usage, "is_complete", False)
        ):
            # Streaming ended before the final usage-bearing event
            # arrived.  Log a structured diagnostic so operators can
            # answer "which providers are dropping the final usage
            # event?" without grepping stdout.
            emit_parse_failure_log(
                _diag_for(
                    provider_id=provider_id,
                    model_id=model_id,
                    protocol=protocol,
                    reason="missing_final_stream_event",
                )
            )
        return normalized

    emit_parse_failure_log(
        _diag_for(
            provider_id=provider_id,
            model_id=model_id,
            protocol=protocol,
            reason="missing_usage_block",
        )
    )
    return NormalizedUsage(cache_counter_status=CacheCounterStatus.UNKNOWN_FORMAT)


def _diag_for(
    *,
    provider_id: str | None,
    model_id: str | None,
    protocol: str,
    reason: str,
) -> Any:
    """Construct a :class:`UsageParseDiag` for a failure-mode log line."""
    return UsageParseDiag(
        provider_id=provider_id,
        model_id=model_id,
        protocol=protocol,
        reason=reason,
        raw_keys=[],
    )


@dataclass(slots=True)
class ProxyRequestContext:
    """Input context for a proxy request."""

    request_id: str
    protocol: str  # 'openai' or 'anthropic'
    model_id: str
    streaming: bool
    original_body: bytes
    incoming_headers: dict[str, str]
    original_body_size: int | None = None
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
    # Earliest ASGI handler entry after auth / body-limit.
    # middleware.  Set by ``handle_proxy_request`` to a monotonic ns
    # timestamp so the total local pre-upstream latency (this field to
    # ``_send_upstream_request``) can be computed.  ``started_monotonic_ns``
    # is set later during context build (after auth, body_read, json_parse,
    # model_parse, context_limit, transcode_preflight, compression policy,
    # segmentation, compression apply, context_build), so the two
    # timestamps together bound all dispatch-prep work.
    request_received_monotonic_ns: int | None = None
    # Total local pre-upstream latency in milliseconds, set just
    # before ``client.send`` in ``_send_upstream_request``.  Distinct
    # from ``dispatch_overhead`` (the coordinator-internal slice).
    local_pre_upstream_ms: int | None = None
    client_metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    response_handoff: ResponseHandoffState = field(default_factory=ResponseHandoffState)
    attempted_accounts: set[str] = field(default_factory=set[str])
    provider_id: str | None = None
    client_ip: str = ""
    upstream_connect_ms: int | None = None
    upstream_headers_ms: int | None = None
    upstream_protocol: str = ""
    transcode_required: bool = False
    transcode_context: TranscodeContext | None = None
    # Plan 143: wire endpoint surface for OpenAI-family requests.
    # ``"chat_completions"`` is the historical default and the only
    # surface that triggers ``stream_options.include_usage`` injection
    # or any Chat Completions-specific transform. ``"responses"`` marks
    # a stateless same-protocol passthrough.
    request_surface: str = "chat_completions"
    thinking_trace: dict[str, Any] | None = None
    thinking_intent: Any | None = None  # ThinkingRequestIntent | None
    segmentation: Any | None = None
    segmentation_not_collected: bool = False
    prepared_transcode: PreparedTranscode | None = None
    # Phase 4.4: precomputed values computed once in handle_proxy_request()
    # so _select_and_persist_attempt() does not reparse original_body.
    estimated_reservation_tokens: int | None = None
    thinking_requirement: Any | None = None  # ThinkingRequestRequirement | None
    estimated_context_input_tokens: int | None = None
    # F7: parsed payload cache — created once, avoids repeated json.loads.
    parsed_payload: ParsedRequestPayload | None = None
    # Plan 028: typed provider-bound lifecycle object.  When set,
    # ``body_for_upstream`` delegates to ``provider_bound.provider_bytes``
    # so the serialized body is produced exactly once after all transforms.
    provider_bound: Any | None = None  # ProviderBoundRequest | None

    def __post_init__(self) -> None:
        if self.original_body_size is None:
            self.original_body_size = len(self.original_body)
        if not self.upstream_protocol:
            self.upstream_protocol = self.protocol

    def release_dispatch_buffers(self) -> None:
        """Release large request-side buffers once the chosen attempt is handed off."""
        self.original_body = b""
        self.parsed_payload = None
        # Prepared translation is only a pre-dispatch reuse aid. Once retry
        # is impossible, release its translated graph and encoded body along
        # with the provider-bound buffers; diagnostics and usage accounting
        # remain on the smaller context objects.
        self.prepared_transcode = None
        if self.provider_bound is not None:
            self.provider_bound.release_dispatch_buffers()

    @property
    def body_for_upstream(self) -> bytes:
        """Return the dispatch body, preserving original client bytes separately.

        Plan 028: when a ``ProviderBoundRequest`` is attached, its
        ``provider_bytes`` (serialized exactly once after the transform
        pipeline) is the authoritative dispatch body.
        """
        if self.provider_bound is None:
            raise RuntimeError("provider-bound request is required before dispatch")
        pb_bytes = self.provider_bound.provider_bytes
        if pb_bytes is None:
            raise RuntimeError("provider-bound request is not serialized")
        return pb_bytes


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
    api_key: str = field(repr=False)
    model_id: str
    estimated_tokens: int
    estimated_microdollars: int
    attempt_number: int
    provider_id: str = DEFAULT_PROVIDER_ID
    requires_transcode: bool = False
    protocol: str = "openai"
    streamed: bool = False
    runtime_lease: AttemptRuntimeLease | None = field(default=None, repr=False)


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
    response_handoff: ResponseHandoffState = field(default_factory=ResponseHandoffState)


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

    @staticmethod
    def _serialize_provider_request(context: ProxyRequestContext) -> bytes:
        """Serialize the final provider generation exactly once."""
        request = context.provider_bound
        if request is None:
            raise RuntimeError("provider-bound request is required for serialization")
        return request.serialize_provider_payload()

    def _validate_serialized_request_size(
        self,
        context: ProxyRequestContext,
        serialized_body: bytes,
        *,
        selected_provider_id: str | None = None,
    ) -> None:
        """Reject locally oversized serialized payloads before upstream dispatch.

        Uses ``max_serialized_request_bytes`` from the resolved multimodal
        capabilities for the selected provider when available.  Collapsed
        models may be served by multiple providers with different limits,
        so the *selected* provider's row is authoritative.  This is a
        local-only validation that returns ``RequestTooLargeError``
        (HTTP 413) and must never penalize or quarantine the provider
        account.
        """
        from eggpool.catalog.capabilities import dict_to_model_capabilities
        from eggpool.errors import RequestTooLargeError

        max_bytes: int | None = None
        model_info = self._catalog.cache.get_model_for_provider(
            context.model_id, selected_provider_id
        )
        if model_info is not None:
            caps_raw: dict[str, Any] = model_info.get("capabilities", {})  # type: ignore[assignment]
            caps = dict_to_model_capabilities(caps_raw)
            max_bytes = caps.multimodal.max_serialized_request_bytes
        if max_bytes is not None and len(serialized_body) > max_bytes:
            raise RequestTooLargeError(
                f"Serialized request body ({len(serialized_body)} bytes) "
                f"exceeds provider limit ({max_bytes} bytes)"
            )

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
        local_pre_upstream_recorder: Any | None = None,  # noqa: ANN401
        dispatch_span_recorder: Any | None = None,  # noqa: ANN401
        transcoder_policy: TranscoderPolicy | None = None,
        stream_diagnostics: StreamDiagnostics | None = None,
        finalization_supervisor: Any | None = None,  # noqa: ANN401
        routing_trace_guard: Any | None = None,  # noqa: ANN401
        routing_trace_enabled: bool = True,
        routing_trace_writer: Any | None = None,  # noqa: ANN401
        selection_claim_diagnostics: SelectionClaimDiagnostics | None = None,
        effects_applier: EffectsApplier | None = None,
        quarantine: ModelQuarantine | None = None,
        account_identities: dict[str, AccountRuntimeIdentity] | None = None,
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
        self._selection_claim_lock = asyncio.Lock()
        self._selection_claim_diagnostics = (
            selection_claim_diagnostics
            if selection_claim_diagnostics is not None
            else get_selection_claim_diagnostics()
        )
        self._account_identities = MappingProxyType(dict(account_identities or {}))
        self._account_id_cache: dict[str, int] = {
            name: identity.account_id
            for name, identity in self._account_identities.items()
        }
        self._account_identities_hydrated = account_identities is not None
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
        self._local_pre_upstream_recorder = local_pre_upstream_recorder
        self._dispatch_span_recorder = dispatch_span_recorder
        self._transcoder_policy = transcoder_policy
        self._stream_diagnostics = stream_diagnostics or get_stream_diagnostics()
        self._finalization_supervisor = finalization_supervisor
        if routing_trace_guard is None and routing_trace_enabled:
            from eggpool.request.routing_trace_guard import RoutingTraceGuard

            routing_trace_guard = RoutingTraceGuard()
        self._routing_trace_guard = routing_trace_guard
        self._routing_trace_writer = routing_trace_writer

        # Plan 025: typed failure effects applier + bounded quarantine.
        # The factory constructs and injects these; legacy tests may
        # instantiate the coordinator without them, so fall back to a
        # default applier that uses the health_manager + catalog cache.
        self._quarantine = quarantine if quarantine is not None else ModelQuarantine()
        if effects_applier is not None:
            self._effects_applier: EffectsApplier | None = effects_applier
        else:
            self._effects_applier = EffectsApplier(
                health_manager=health_manager,
                quarantine=self._quarantine,
                catalog_cache=catalog.cache,
            )

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
            effects_applier=self._effects_applier,
            quarantine=self._quarantine,
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

    async def _finalize_terminal(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        data: FinalizationData,
    ) -> None:
        """Submit the single retained owner for a selected terminal outcome.

        The supervisor registration is deliberately part of this helper so
        normal completion, cancellation, client errors, and upstream errors
        cannot accidentally take different cleanup paths.
        """
        supervisor = self._finalization_supervisor
        if supervisor is None:
            raise AcceptedFinalizationInvariantError(
                "terminal finalization requires the generation supervisor",
                step="finalization_owner",
                request_id=context.request_id,
            )
        if data.failure_effects is not None and data.effect_progress is None:
            data.effect_progress = FailureEffectProgress(
                attempt_key=f"{selected.proxy_request_id}:{selected.attempt_id}"
            )
        runtime_lease = selected.runtime_lease or AttemptRuntimeLease(
            account_name=selected.account_name,
        )
        runtime_lease.bind_outcome_obligations(
            usage_required=True,
            health_required=not data.health_already_applied,
            account_runtime_required=not data.health_already_applied,
        )
        from eggpool.request.finalization_job import FinalizationIdentity

        identity = FinalizationIdentity(
            proxy_request_id=context.request_id,
            db_request_id=selected.db_request_id,
            attempt_id=selected.attempt_id,
            reservation_id=selected.reservation_id,
            account_id=selected.account_id,
            account_name=selected.account_name,
            provider_id=selected.provider_id,
            model_id=selected.model_id,
            client_protocol=context.protocol,
            upstream_protocol=context.upstream_protocol,
            attempt_number=selected.attempt_number,
        )
        try:
            job = supervisor.register_or_get(
                identity,
                data.outcome.value,
                finalization_data=data,
                runtime_lease=runtime_lease,
                failure_effects=data.failure_effects,
            )
        except FinalizationCapacityError as exc:
            logger.error(
                "Finalization supervisor capacity rejected terminal ownership: "
                "request_id=%s attempt_id=%s outcome=%s downstream_started=%s "
                "bytes_emitted=%s",
                context.request_id,
                selected.attempt_id,
                data.outcome.value,
                data.downstream_started,
                data.bytes_emitted,
            )
            if not data.downstream_started:
                raise AcceptedFinalizationInvariantError(
                    "terminal finalization capacity exhausted before handoff",
                    step="finalization_capacity",
                    request_id=context.request_id,
                ) from exc
            # Ownership was never transferred, so release every runtime
            # component this request still holds (active count, quota
            # reservation, health probe). Skipping any of them leaks the
            # account's active count / reserved total until restart.
            for outcome_row in await runtime_lease.release_once(
                reason="finalization_capacity_rejected",
                router=self._router,
                quota_estimator=self._quota_estimator,
                health_manager=self._health_manager,
            ):
                if not outcome_row.released:
                    logger.error(
                        "Runtime lease release failed after finalization "
                        "capacity rejection: request_id=%s component=%s error=%s",
                        context.request_id,
                        outcome_row.component,
                        outcome_row.error,
                    )
            return
        job.set_dependencies(
            finalizer=self._finalizer,
            selected=selected,
            effects_applier=self._effects_applier,
            router=self._router,
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
            stream_diagnostics=self._stream_diagnostics,
        )
        await job.run()

    async def _cleanup_failed_attempt(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        error: _RetryableUpstreamError,
    ) -> None:
        """Submit cleanup and wait for convergence before reselection."""
        supervisor = self._finalization_supervisor
        if supervisor is None:
            raise AcceptedFinalizationInvariantError(
                "failed-attempt cleanup requires the generation supervisor",
                step="failed_attempt_cleanup_owner",
                request_id=context.request_id,
            )
        identity = FinalizationIdentity(
            proxy_request_id=selected.proxy_request_id,
            db_request_id=selected.db_request_id,
            attempt_id=selected.attempt_id,
            reservation_id=selected.reservation_id,
            account_id=selected.account_id,
            account_name=selected.account_name,
            provider_id=selected.provider_id,
            model_id=selected.model_id,
            client_protocol=context.protocol,
            upstream_protocol=context.upstream_protocol,
            attempt_number=selected.attempt_number,
        )
        submission = FailedAttemptCleanupSubmission(
            identity=identity,
            status_code=error.status_code,
            error_class=error.error_class,
            retry_category=(
                error.retry_category.value if error.retry_category is not None else None
            ),
            bytes_received=context.original_body_size or len(context.original_body),
            latency_ms=self._elapsed_ms(context),
            failure_effects=error.failure_effects,
        )

        async def run_cleanup(
            submitted: object, progress: TerminalCommandProgress
        ) -> None:
            if not isinstance(submitted, FailedAttemptCleanupSubmission):
                raise RuntimeError("failed-attempt cleanup submission type mismatch")
            if not isinstance(progress, FailedAttemptCleanupProgress):
                raise RuntimeError("failed-attempt cleanup progress type mismatch")
            if progress.effect_progress is None:
                progress.effect_progress = FailureEffectProgress(
                    attempt_key=f"{selected.proxy_request_id}:{selected.attempt_id}"
                )
            await self._run_failed_attempt_cleanup(
                context=context,
                selected=selected,
                error=error,
                submission=submitted,
                progress=progress,
            )

        command = supervisor.register_failed_attempt_cleanup(submission, run_cleanup)
        await supervisor.run_terminal_command(command)
        if not command.is_complete:
            raise RuntimeError("failed-attempt cleanup did not converge")

    async def _run_failed_attempt_cleanup(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        error: _RetryableUpstreamError,
        submission: FailedAttemptCleanupSubmission,
        progress: FailedAttemptCleanupProgress,
    ) -> None:
        if (
            not progress.durable_transition_checked
            or not progress.durable_reservation_converged
        ):
            result = await self._attempt_finalizer.finalize_failed_attempt(
                attempt_id=selected.attempt_id,
                reservation_id=selected.reservation_id,
                data=AttemptFinalizationData(
                    request_id=selected.db_request_id,
                    status_code=submission.status_code,
                    error_class=submission.error_class,
                    release_reason="attempt_retryable",
                    retry_category=submission.retry_category,
                    bytes_received=submission.bytes_received,
                    latency_ms=submission.latency_ms,
                    is_retry_outcome=True,
                ),
            )
            # Record durable facts before any runtime release await.  A later
            # rejoin must never use ``attempt_transitioned`` as a reason to
            # skip unfinished runtime cleanup.
            progress.durable_transition_checked = True
            progress.durable_attempt_transitioned = result.attempt_transitioned
            progress.durable_reservation_converged = result.reservation_converged
            progress.runtime_cleanup_required = result.reservation_released

        if not progress.runtime_cleanup_required:
            # Another terminal owner already completed the durable attempt or
            # reservation. Its finalization path owns the runtime release too.
            # Mark these components converged without replaying them here.
            progress.quota_released = True
            progress.active_count_released = True
            progress.health_effect_applied = True
            progress.probe_released = True

        if (
            progress.runtime_cleanup_required
            and not progress.quota_released
            and (self._quota_estimator is not None)
        ):
            await self._quota_estimator.remove_reservation(
                selected.account_name,
                selected.estimated_microdollars,
                requests=1,
                tokens=selected.estimated_tokens,
            )
            progress.quota_released = True
        elif not progress.runtime_cleanup_required or self._quota_estimator is None:
            progress.quota_released = True

        if progress.runtime_cleanup_required and not progress.active_count_released:
            await self._router.decrement_active_request_count(selected.account_name)
            progress.active_count_released = True

        if progress.runtime_cleanup_required and not progress.health_effect_applied:
            await self._apply_health_transition(
                selected.account_name,
                error,
                context.model_id,
                provider_id=selected.provider_id,
                upstream_protocol=context.upstream_protocol,
                client_protocol=context.protocol,
                effect_progress=progress.effect_progress,
            )
            progress.health_effect_applied = True
            # Every health transition path either records a success/failure,
            # which clears a half-open probe, or explicitly releases it.
            progress.probe_released = True
        elif not progress.probe_released:
            progress.probe_released = True

        progress.completed = all(
            (
                progress.durable_transition_checked,
                progress.durable_reservation_converged,
                progress.quota_released,
                progress.active_count_released,
                progress.health_effect_applied,
                progress.probe_released,
            )
        )

    async def _join_attempt_cleanup(self, key: tuple[str, int]) -> bool:
        """Join supervisor-owned attempt cleanup and report convergence."""
        supervisor = self._finalization_supervisor
        if supervisor is None:
            raise AcceptedFinalizationInvariantError(
                "attempt cleanup requires the generation supervisor",
                step="attempt_cleanup_owner",
                request_id=key[0],
            )
        command = supervisor.get_terminal_command(
            key[0], key[1], "failed_attempt_cleanup"
        )
        if command is None:
            return True
        try:
            await supervisor.run_terminal_command(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Supervisor-owned attempt cleanup failed while joining: "
                "request_id=%s attempt_id=%s",
                key[0],
                key[1],
            )
            return False
        return command.is_complete

    async def _finalize_cancelled_after_cleanup(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        health_already_applied: bool,
    ) -> None:
        """Submit the canonical request terminal after ownership converges."""
        if context.client_metadata.get("_cancelled_request_finalized"):
            return
        context.client_metadata["_cancelled_request_finalized"] = True
        await self._finalize_terminal(
            context,
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.CLIENT_CANCELLED,
                error_class="CancelledError",
                upstream_latency_ms=self._elapsed_ms(context),
                bytes_received=context.original_body_size or len(context.original_body),
                downstream_started=context.response_handoff.started,
                health_already_applied=health_already_applied,
                upstream_protocol=context.upstream_protocol,
                thinking_trace_json=_serialize_thinking_trace(context.thinking_trace),
            ),
        )

    async def _await_cleanup_then_finalize_cancelled(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
    ) -> bool:
        """Rejoin retained cleanup before terminalizing a cancelled request."""
        converged = await self._join_attempt_cleanup(
            (selected.proxy_request_id, selected.attempt_id)
        )
        if not converged:
            logger.error(
                "Cancelled request cleanup has not converged; request remains "
                "available for bounded rejoin: request_id=%s attempt_id=%s",
                selected.proxy_request_id,
                selected.attempt_id,
            )
            return False
        await self._finalize_cancelled_after_cleanup(
            context=context,
            selected=selected,
            health_already_applied=True,
        )
        return True

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
            max_logged_warnings = 32
            logged_warnings = warnings[:max_logged_warnings]
            omitted_warnings = max(0, len(warnings) - len(logged_warnings))
            logger.info(
                "transcode.loss_warnings request_id=%s "
                "client=%s upstream=%s warnings=%s omitted=%d",
                context.request_id,
                context.protocol,
                context.upstream_protocol,
                logged_warnings,
                omitted_warnings,
            )
        # Phase 5: per-request structured log for every transcoded request
        if selected is not None:
            logger.debug(
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
        """Execute a request behind one ordinary-exception safety boundary.

        Stage-specific dispatch code handles expected transport and protocol
        outcomes.  This outer boundary contains an unexpected local defect so
        it cannot escape as an unhandled ASGI exception or strand a selected
        attempt.  Cancellation and the established public error hierarchy
        remain transparent to callers.
        """
        try:
            return await self._execute_impl(context)
        except asyncio.CancelledError:
            raise
        except AggregatorError:
            raise
        except (ProtocolMismatchError, TranscodeLossError):
            raise
        except Exception as err:
            logger.exception(
                "Unexpected local proxy exception: request_id=%s protocol=%s "
                "model=%s exception=%s",
                context.request_id,
                context.protocol,
                context.model_id,
                type(err).__name__,
            )
            selected = context.client_metadata.get("_post_commit_selected")
            if isinstance(selected, SelectedAttempt):
                try:
                    await self._finalize_unexpected_local_error(
                        context=context,
                        selected=selected,
                        error=err,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Unexpected local exception cleanup failed: "
                        "request_id=%s attempt_id=%s",
                        context.request_id,
                        selected.attempt_id,
                    )
                    try:
                        await self._schedule_unexpected_local_cleanup(
                            context=context,
                            selected=selected,
                            error=err,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Failed to schedule unexpected local cleanup: "
                            "request_id=%s attempt_id=%s",
                            context.request_id,
                            selected.attempt_id,
                        )
            return self._build_local_error_response(context, status_code=500)

    async def _execute_impl(
        self, context: ProxyRequestContext
    ) -> PreparedProxyResponse:
        """Execute a request through the full lifecycle.

        Returns a PreparedProxyResponse with either body (non-streaming)
        or stream_iterator (streaming). On retryable pre-body failures,
        retries on different accounts (excluding previously attempted ones).
        """
        # Section 10.5: Validate endpoint before durable selection.
        # Reject mismatched protocol endpoints before creating any
        # request, reservation, or attempt row.
        self._validate_endpoint_or_transcode(context)
        provider_bound = context.provider_bound
        if provider_bound is None:
            # Compatibility for direct coordinator tests and embedders that
            # construct ProxyRequestContext themselves. The API handler
            # always supplies the canonical object from its single parse.
            provider_bound = self._legacy_provider_request(context)
            context.provider_bound = provider_bound

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
                # Plan 141: text-only requests with no provider-sensitive
                # multimodal content may reuse the preflight translation
                # immediately so ordinary cross-protocol requests do not pay
                # the post-selection translation cost. The prepared-transcode
                # fast path is bounded by ``is_valid_for`` (upstream
                # protocol + transcoder features match) and by the absence
                # of provider-sensitive media in the original client payload.
                _prepared = context.prepared_transcode
                _features = (
                    self._transcoder_policy.features
                    if self._transcoder_policy is not None
                    else None
                )
                _thinking_off = _features is None or not getattr(
                    _features, "thinking", False
                )
                _client_has_thinking = self._client_has_thinking_controls(
                    context.original_body,
                    context.protocol,
                    parsed_payload=context.parsed_payload,
                )
                _has_provider_sensitive_media = (
                    self._client_payload_has_provider_sensitive_media(context)
                )
                if (
                    _prepared is not None
                    and _prepared.is_valid_for(
                        upstream_protocol=context.transcode_context.upstream_protocol,
                        features=_features,
                    )
                    and (_thinking_off or not _client_has_thinking)
                    and not _has_provider_sensitive_media
                ):
                    # Reuse the cached preflight translation.
                    # The transcoder owns this request-local generation. Use
                    # Plan 114's trusted adoption boundary so reuse does not
                    # recursively rematerialize the translated graph; any
                    # later provider-specific mutation must establish its own
                    # COW/owned graph before changing it.
                    provider_bound.adopt_provider_payload(
                        _prepared.translated_payload,
                        reason="prepared_transcode",
                    )
                    provider_bound.set_provider_bytes(_prepared.translated_body)
                    provider_bound.diagnostics.provider_decodes += 0
                    context.transcode_context.loss_warnings.extend(
                        dict(warning) for warning in _prepared.warnings
                    )
                    # Dispatch fields are frozen; only the diagnostics object
                    # is updated here for observability.
                    diagnostics = _prepared.diagnostics
                    diagnostics.reused = True
                    logger.debug(
                        "prepared_transcode_reused request_id=%s client=%s upstream=%s "
                        "available=%s reused=%s recompute_reason=%s",
                        context.request_id,
                        context.protocol,
                        context.upstream_protocol,
                        diagnostics.available,
                        diagnostics.reused,
                        diagnostics.recompute_reason,
                    )
                elif _prepared is not None:
                    # Record the pre-selection recompute reason for
                    # observability. The actual definitive translation is
                    # deferred until after ``SelectedAttempt`` exists so the
                    # capability row of the *selected* provider is
                    # authoritative; see ``_apply_selected_provider_transcode``.
                    if _has_provider_sensitive_media:
                        _recompute_reason = "provider_multimodal_capability_required"
                    elif _client_has_thinking:
                        _recompute_reason = "thinking_controls_present"
                    else:
                        _recompute_reason = "protocol_or_features_mismatch"
                    diagnostics = _prepared.diagnostics
                    diagnostics.reused = False
                    diagnostics.recompute_reason = _recompute_reason
                    logger.debug(
                        "prepared_transcode_deferred_to_selection "
                        "request_id=%s reason=%s",
                        context.request_id,
                        _recompute_reason,
                    )
                else:
                    logger.debug(
                        "prepared_transcode_deferred_to_selection "
                        "request_id=%s reason=no_prepared_result",
                        context.request_id,
                    )

                # Native path: thinking controls pass through unchanged.
                # Phase D: when transcoding is disabled but the client
                # still asked for thinking, mark the trace as
                # passthrough so observability surfaces the decision.
                if context.thinking_trace is not None:
                    decision_value = context.thinking_trace.get("decision", "none")
                    if decision_value == "none":
                        context.thinking_trace["decision"] = "passthrough"
                        context.thinking_trace["upstream_protocol"] = (
                            context.upstream_protocol
                        )
                    elif decision_value == "passthrough":
                        context.thinking_trace["upstream_protocol"] = (
                            context.upstream_protocol
                        )
            else:
                # Transcoder unavailable — mark any prepared transcode as
                # not reused with a reason so operators can see why. The
                # frozen dispatch payload stays untouched.
                _prepared = context.prepared_transcode
                if _prepared is not None:
                    diagnostics = _prepared.diagnostics
                    diagnostics.reused = False
                    diagnostics.recompute_reason = "transcoder_missing"

        last_error: Exception | None = None
        last_upstream_response: tuple[int, list[tuple[str, str]], bytes] | None = None
        attempt_num = 0
        last_selected: SelectedAttempt | None = None
        last_converged_selected: SelectedAttempt | None = None
        health_applied = False

        for attempt_num in range(1, self._max_retry_attempts + 1):
            try:
                selected = await self._select_and_persist_attempt(context, attempt_num)
            except asyncio.CancelledError:
                try:
                    await self._handle_selection_cancellation(
                        context,
                        fallback_selected=last_converged_selected,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to terminalize cancellation after selection "
                        "cleanup: request_id=%s",
                        context.request_id,
                    )
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
            except _LocalDispatchError as err:
                last_error = err
                last_upstream_response = None
                logger.exception(
                    "Local dispatch stage failed: request_id=%s stage=%s",
                    context.request_id,
                    err.stage,
                    exc_info=err.__cause__,
                )
                break
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
            # Plan 141/142: after ``SelectedAttempt`` exists, perform the
            # definitive cross-protocol translation against the selected
            # provider's capability row. This applies when the preflight
            # produced no reusable :class:`PreparedTranscode` (provider-
            # sensitive media, thinking controls, or no preflight). Text-
            # only requests with a valid prepared-transcode already adopted
            # the preflight translation in pre-selection and skip this.
            if transcoder is not None:
                try:
                    await self._apply_selected_provider_transcode(
                        context=context,
                        selected=selected,
                        transcoder=transcoder,
                    )
                except asyncio.CancelledError:
                    raise
                except CapabilityError as err:
                    # Plan 142: a selected-provider capability check
                    # (e.g. thinking-budget rejection, modality/control
                    # incompatibility) is a client-validation outcome, not
                    # an internal defect. Converge the selected attempt
                    # through the canonical capability-rejection terminal
                    # owner, then re-raise the typed error so the API
                    # renderer renders it as 400. No retry, no provider
                    # health/backoff/quarantine effect.
                    try:
                        await self._finalize_selected_capability_rejection(
                            context=context,
                            selected=selected,
                            err=err,
                        )
                    except (
                        AcceptedFinalizationInvariantError,
                        DatabaseError,
                    ) as finalize_err:
                        # Plan 142: fail closed. Do not silently ignore a
                        # durable finalization failure; the existing 500
                        # fallback in ``RequestCoordinator.execute`` and
                        # ``_handle_proxy_request_inner`` will own the
                        # response rather than reporting a clean 400 when
                        # convergence is unknown.
                        raise finalize_err from err
                    raise
                except TranscodeLossError as err:
                    # Plan 142: the transcoder's strict ``loss_policy =
                    # "reject"`` (or per-feature loss) decided that the
                    # selected provider cannot represent the client's
                    # request. This is a client-validation outcome;
                    # converge the selected attempt through the canonical
                    # transcode-loss terminal owner, then re-raise so the
                    # API renderer renders it as 400. No retry, no
                    # provider health/backoff/quarantine effect, no
                    # upstream HTTP request is built.
                    try:
                        await self._finalize_selected_transcode_loss_rejection(
                            context=context,
                            selected=selected,
                            err=err,
                        )
                    except (
                        AcceptedFinalizationInvariantError,
                        DatabaseError,
                    ) as finalize_err:
                        raise finalize_err from err
                    raise
                except Exception as err:
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="selected_provider_transcode",
                        error=err,
                    ) from err
            try:
                result = await self._execute_upstream(
                    context, selected, attempt_num, transcoder=transcoder
                )
                self._log_transcode_warnings(context, selected=selected)
                return result
            except _RetryableUpstreamError as err:
                last_error = err
                if context.response_handoff.started:
                    raise AcceptedFinalizationInvariantError(
                        "retry reached a started downstream response",
                        step="retry_after_response_start",
                        request_id=context.request_id,
                    ) from err
                # Track the last useful upstream response
                if err.upstream_response is not None:
                    last_upstream_response = err.upstream_response
                logger.warning(
                    "Retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                try:
                    await self._cleanup_failed_attempt(
                        context=context,
                        selected=selected,
                        error=err,
                    )
                except asyncio.CancelledError:
                    try:
                        await self._await_cleanup_then_finalize_cancelled(
                            context=context,
                            selected=selected,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Failed to terminalize cancellation after attempt "
                            "cleanup: request_id=%s attempt_id=%s",
                            selected.proxy_request_id,
                            selected.attempt_id,
                        )
                    raise
                last_converged_selected = selected
                for key in _ATTEMPT_SELECTION_METADATA_KEYS:
                    context.client_metadata.pop(key, None)
                health_applied = True
                if err.failure_effects is not None and not err.failure_effects.retry:
                    break
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
                    if remaining:
                        context.client_metadata["attempt_ceiling_reached"] = True
                        logger.info(
                            "Request retry ceiling reached: request_id=%s "
                            "attempt=%d remaining_accounts=%d",
                            context.request_id,
                            attempt_num,
                            len(remaining),
                        )
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
                # failure through the finalizer.  Plan 025: when an
                # effects applier is wired, route through the typed
                # effects classifier so the same decision table
                # governs retryable and non-retryable failures.
                if (
                    self._effects_applier is not None
                    and self._health_manager is not None
                ):
                    observation = (
                        err.failure_observation
                        or self._build_failure_observation(
                            context=context,
                            selected=selected,
                            status_code=err.status_code,
                            body=(
                                err.upstream_response[2]
                                if err.upstream_response
                                else None
                            ),
                            error_class=err.error_class,
                        )
                    )
                    effects = err.failure_effects or classify_failure_effects(
                        observation
                    )
                    attempt_key = (
                        f"{observation.proxy_request_id or selected.account_name}:"
                        f"{observation.attempt_id or err.status_code or 'unselected'}"
                    )
                    self._effects_applier.apply_once(
                        attempt_key=attempt_key,
                        observation=observation,
                        effects=effects,
                        progress=FailureEffectProgress(attempt_key=attempt_key),
                    )
                    health_applied = True
                    if effects.persist_backoff and effects.backoff_reason:
                        await self._persist_backoff(
                            account_name=selected.account_name,
                            model_id=context.model_id
                            if effects.model_effect
                            in ("quarantine", "terminal_withdrawal")
                            else None,
                            reason=effects.backoff_reason,
                            status_code=err.status_code,
                            error_class=err.error_class,
                            backoff_until=effects.backoff_until,
                            consecutive_failures=(
                                self._health_manager.get_account_health(
                                    selected.account_name
                                ).consecutive_failures
                            ),
                        )
                elif self._health_manager is not None:
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
                        # Prefer a provider Retry-After carried by the
                        # failure effects; default to a 60 s cooldown.
                        carried_retry_after = (
                            err.failure_effects.retry_after_s
                            if err.failure_effects is not None
                            else None
                        )
                        self._health_manager.record_rate_limit(
                            selected.account_name,
                            60.0
                            if carried_retry_after is None
                            else carried_retry_after,
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

    @dataclass(slots=True, frozen=True)
    class _ClaimIdentity:
        """Immutable per-attempt identity snapshot taken under
        ``_selection_claim_lock`` and reused after the lock releases.

        Milestone B extracts the read-only identity fields computed
        inside the first lock acquisition (API key, account id,
        provider id, reservation cost) into a small frozen value so
        they can flow through the database-persist step and the
        publication step without holding ``_selection_claim_lock``
        open.  Only ``api_key`` and ``account_name`` are needed by
        ``_execute_upstream``; the rest are diagnostic / persistence
        hooks.
        """

        account_name: str
        account_id: int
        resolved_provider_id: str
        api_key: str
        estimated_microdollars: int

    async def _persist_dispatch_bundle(
        self,
        *,
        context: ProxyRequestContext,
        account_id: int,
        resolved_provider_id: str,
        estimated_tokens: int,
        estimated_microdollars: int,
        attempt_number: int,
    ) -> tuple[str, str, int]:
        """Create / update the request, reservation, and attempt rows.

        This canonical DB-I/O phase runs OUTSIDE
        ``_selection_claim_lock``. Each row write is recorded under
        ``SPAN_DB_WRITE_*``; ``SPAN_DISPATCH_PERSISTENCE_*`` covers the
        transaction and commit boundaries outside the selection critical
        section.
        """

        if (
            self._request_repo is None
            or self._reservation_repo is None
            or self._attempt_repo is None
        ):
            raise DatabaseError("Cannot persist: database repositories unavailable")

        created_request = "db_request_id" not in context.client_metadata
        if created_request:
            first_attempt_at = time.time() if attempt_number == 1 else None
            with _maybe_span(self._dispatch_span_recorder, SPAN_DB_WRITE_REQUEST):
                db_request_id = await self._request_repo.create_pending(
                    request_id=context.request_id,
                    model_id=context.model_id,
                    protocol=context.protocol,
                    streamed=context.streaming,
                    account_id=account_id,
                    reserved_microdollars=estimated_microdollars,
                    started_at=context.started_at,
                    first_attempt_at=first_attempt_at,
                    provider_id=resolved_provider_id,
                    client_ip=context.client_ip,
                )
            context.client_metadata["db_request_id"] = db_request_id
        else:
            db_request_id = context.client_metadata["db_request_id"]
            with _maybe_span(self._dispatch_span_recorder, SPAN_DB_WRITE_REQUEST):
                await self._request_repo.update_after_selection(
                    request_id=db_request_id,
                    account_id=account_id,
                    reserved_microdollars=estimated_microdollars,
                )

        with _maybe_span(self._dispatch_span_recorder, SPAN_DB_WRITE_RESERVATION):
            reservation_id = await self._reservation_repo.create(
                request_id=db_request_id,
                account_id=account_id,
                model_id=context.model_id,
                estimated_tokens=estimated_tokens,
                estimated_microdollars=estimated_microdollars,
            )
        with _maybe_span(self._dispatch_span_recorder, SPAN_DB_WRITE_ATTEMPT):
            attempt_id = await self._attempt_repo.create(
                request_id=db_request_id,
                attempt_number=attempt_number,
                account_id=account_id,
                provider_id=resolved_provider_id,
                model_id=context.model_id,
                protocol=context.protocol,
                streamed=context.streaming,
            )

        self._selection_claim_diagnostics.record_claim_committed()
        return db_request_id, reservation_id, attempt_id

    def _release_unpublished_claim(
        self,
        *,
        account_name: str,
        estimated_tokens: int,
        estimated_microdollars: int = 0,
        receipt: RuntimePublicationReceipt,
    ) -> None:
        """Release provisional claim ownership before durable publication.

        Delegates to :func:`claim_lifecycle.release_unpublished_claim`.
        """
        from eggpool.request.claim_lifecycle import release_unpublished_claim

        release_unpublished_claim(
            account_name=account_name,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=estimated_microdollars,
            receipt=receipt,
            quota_estimator=self._quota_estimator,
            health_manager=self._health_manager,
        )

    async def _publish_runtime_state(
        self,
        *,
        account_name: str,
        estimated_tokens: int,
        estimated_microdollars: int,
        receipt: RuntimePublicationReceipt,
    ) -> None:
        """Convert pending load and publish canonical runtime ownership.

        This runs INSIDE the brief second acquisition of
        ``_selection_claim_lock`` so a concurrent selector observes either
        the provisional claim or its canonical reservation. Database I/O has
        already committed at this point.
        """

        if receipt.pending_request_added and not receipt.pending_load_converted:
            estimator = self._quota_estimator
            if estimator is None:
                raise RuntimeError(
                    "pending claim conversion requires the quota estimator"
                )
            convert_pending = getattr(estimator, "convert_pending_claim", None)
            if not callable(convert_pending):
                raise RuntimeError("quota estimator cannot convert pending claims")
            convert_pending(
                account_name,
                estimated_microdollars,
                tokens=estimated_tokens,
            )
            receipt.pending_load_converted = True
            receipt.quota_reservation_added = True

        await self._router.increment_active_request_count(account_name)
        receipt.active_count_added = True
        if self._quota_estimator is not None and not receipt.quota_reservation_added:
            await self._quota_estimator.add_reservation(
                account_name,
                estimated_microdollars,
                requests=1,
                tokens=estimated_tokens,
            )
            receipt.quota_reservation_added = True
        self._selection_claim_diagnostics.record_claim_published()

    async def _compensate_or_rollback_claim(
        self,
        *,
        context: ProxyRequestContext,
        claim_identity: _ClaimIdentity,
        db_request_id: str | None,
        attempt_id: int | None,
        reservation_id: str | None,
        attempt_number: int,
        estimated_tokens: int,
        error: BaseException,
        receipt: RuntimePublicationReceipt,
    ) -> None:
        """Undo partial runtime state on post-commit publication failure.

        Mirrors the original compensating behavior: release any
        unconverted provisional load, decrement the active count if
        publication already incremented it, ask the attempt finalizer to
        mark the attempt ``PostCommitInterrupted``, release the
        circuit-breaker slot, and tag
        ``context.client_metadata`` so downstream diagnostics know
        the publish failed after commit.  The original exception is
        re-raised by the caller after this method returns.
        """

        supervisor = self._finalization_supervisor
        if supervisor is None:
            raise AcceptedFinalizationInvariantError(
                "claim compensation requires the generation supervisor",
                step="claim_compensation_owner",
                request_id=context.request_id,
            )
        identity = FinalizationIdentity(
            proxy_request_id=context.request_id,
            db_request_id=db_request_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            account_id=claim_identity.account_id,
            account_name=claim_identity.account_name,
            provider_id=claim_identity.resolved_provider_id,
            model_id=context.model_id,
            client_protocol=context.protocol,
            upstream_protocol=context.upstream_protocol,
            attempt_number=attempt_number,
        )
        submission = ClaimCompensationSubmission(
            identity=identity,
            account_name=claim_identity.account_name,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=claim_identity.estimated_microdollars,
            bytes_received=context.original_body_size or len(context.original_body),
            latency_ms=self._elapsed_ms(context),
            receipt=receipt,
        )

        async def run_compensation(
            submitted: object, progress: TerminalCommandProgress
        ) -> None:
            if not isinstance(submitted, ClaimCompensationSubmission):
                raise RuntimeError("claim compensation submission type mismatch")
            if not isinstance(progress, ClaimCompensationProgress):
                raise RuntimeError("claim compensation progress type mismatch")
            await self._run_claim_compensation(
                context=context,
                submission=submitted,
                progress=progress,
            )

        command = supervisor.register_claim_compensation(submission, run_compensation)
        try:
            await supervisor.run_terminal_command(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._selection_claim_diagnostics.record_compensation(success=False)
            raise
        if not command.is_complete:
            self._selection_claim_diagnostics.record_compensation(success=False)
            raise RuntimeError("claim compensation did not converge")
        context.client_metadata["post_commit_interrupted"] = True
        self._selection_claim_diagnostics.record_compensation(success=True)
        del error

    async def _run_claim_compensation(
        self,
        *,
        context: ProxyRequestContext,
        submission: ClaimCompensationSubmission,
        progress: ClaimCompensationProgress,
    ) -> None:
        """Release a committed claim one acquired component at a time.

        Delegates to :func:`claim_lifecycle.run_claim_compensation`.
        """
        from eggpool.request.claim_lifecycle import run_claim_compensation

        await run_claim_compensation(
            submission=submission,
            progress=progress,
            quota_estimator=self._quota_estimator,
            router=self._router,
            attempt_finalizer=self._attempt_finalizer,
            health_manager=self._health_manager,
        )
        if progress.completed:
            context.client_metadata["post_commit_interrupted"] = True

    async def _join_claim_compensation(self, key: tuple[str, int]) -> bool:
        """Join supervisor-owned claim compensation and report convergence."""
        supervisor = self._finalization_supervisor
        if supervisor is None:
            return True
        command = supervisor.get_terminal_command(key[0], key[1], "claim_compensation")
        if command is None:
            return True
        try:
            await supervisor.run_terminal_command(command)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Supervisor-owned claim compensation failed while joining: "
                "request_id=%s attempt_id=%s",
                key[0],
                key[1],
            )
            return False
        return command.is_complete

    async def _handle_selection_cancellation(
        self,
        context: ProxyRequestContext,
        *,
        fallback_selected: SelectedAttempt | None = None,
    ) -> bool:
        """Converge a committed selection before terminalizing cancellation."""
        selected_obj = context.client_metadata.get("_post_commit_selected")
        if not isinstance(selected_obj, SelectedAttempt):
            if fallback_selected is None:
                return False
            await self._finalize_cancelled_after_cleanup(
                context=context,
                selected=fallback_selected,
                health_already_applied=True,
            )
            return True
        key = (selected_obj.proxy_request_id, selected_obj.attempt_id)
        supervisor = self._finalization_supervisor
        command = (
            supervisor.get_terminal_command(key[0], key[1], "claim_compensation")
            if supervisor is not None
            else None
        )
        compensation_started = command is not None
        if compensation_started:
            if not await self._join_claim_compensation(key):
                logger.error(
                    "Cancelled selection compensation has not converged: "
                    "request_id=%s attempt_id=%s",
                    key[0],
                    key[1],
                )
                return False
            health_already_applied = bool(
                context.client_metadata.get("post_commit_interrupted")
            )
        elif context.client_metadata.get("post_commit_interrupted"):
            health_already_applied = True
        elif context.client_metadata.get("post_commit_published"):
            # Publication completed and no compensation was needed.  The
            # request finalizer still owns the active/quota/probe release.
            health_already_applied = False
        else:
            return False

        await self._finalize_cancelled_after_cleanup(
            context=context,
            selected=selected_obj,
            health_already_applied=health_already_applied,
        )
        return True

    async def _select_and_persist_attempt(
        self,
        context: ProxyRequestContext,
        attempt_number: int,
    ) -> SelectedAttempt:
        """Atomically select an account, create request/reservation/attempt.

        Ordering invariants:

        1. The first ``_selection_claim_lock`` acquisition revalidates the
           selected account, resolves in-memory identity, and publishes
           provisional request/token load. No SQLite I/O occurs under it.
        2. Durable request/reservation/attempt persistence runs outside the
           claim lock. Its transaction commits before the second claim-lock
           acquisition converts provisional load to canonical ownership.
        3. ``_execute_upstream`` and all upstream I/O happen outside the
           claim locks.

        Phase 5: thinking classification, reservation-token estimate,
        capability policy resolution, and routing-plan construction
        are pure computations that read no mutable runtime state.
        They run before the claim lock so the lock only holds the
        correctness-critical work (circuit probe, account-ID lookup,
        pending-load publication, and runtime conversion).  The plan
        invariants are preserved: the in-process active counter,
        provisional/canonical quota mirrors, and circuit-breaker state are
        the mutable runtime inputs to selection, and those remain serialized
        under the claim lock.

        Compensation: if persistence fails before durable identity
        publication, provisional load and the health slot are released. If
        publication fails after the durable commit, canonical runtime state
        is released, any unconverted provisional load is released, the
        attempt is finalized as ``PostCommitInterrupted``, and the health
        slot is released. The outer ``except BaseException`` catches
        ``CancelledError`` / ``SystemExit`` / ``KeyboardInterrupt`` and
        re-raises them after compensation so they cannot be swallowed.
        """
        if (
            self._request_repo is None
            or self._reservation_repo is None
            or self._attempt_repo is None
        ):
            raise DatabaseError("Cannot persist: database repositories unavailable")

        # Legacy/test coordinators may not be built by the runtime-generation
        # factory. Hydrate once, before any claim lock is acquired, so the
        # production path remains entirely in-memory during the claim.
        if not self._account_identities_hydrated:
            rows = await AccountRepository(self._db).list_enabled()
            identities: dict[str, AccountRuntimeIdentity] = {}
            for row in rows:
                name = str(row["name"])
                state = self._registry.get_state(name)
                identities[name] = AccountRuntimeIdentity(
                    account_id=int(row["id"]),
                    account_name=name,
                    provider_id=str(row.get("provider_id") or DEFAULT_PROVIDER_ID),
                    has_usable_credentials=self._registry.has_usable_credentials(name),
                    routing_priority=state.routing_priority if state else 0,
                    weight=float(row.get("weight", 1.0)),
                )
            self._account_identities = MappingProxyType(identities)
            self._account_id_cache = {
                name: identity.account_id for name, identity in identities.items()
            }
            self._account_identities_hydrated = True

        # 0. Pre-lock computation: classify thinking requirement, estimate
        # reservation tokens, and build the routing plan.  These are pure
        # functions of the request body and the static registry/catalog
        # state — moving them outside the lock removes the biggest
        # non-correctness contributor to lock hold time.
        from eggpool.catalog.capabilities import classify_thinking_request

        with _maybe_span(self._dispatch_span_recorder, SPAN_THINKING_CLASSIFICATION):
            if context.thinking_requirement is not None:
                thinking_req = context.thinking_requirement
            else:
                # Fallback for tests/legacy callers that bypass
                # handle_proxy_request precomputation.  Use the cached
                # parsed dict when available to avoid re-parsing.
                body_dict: dict[str, object] = {}
                if context.parsed_payload is not None:
                    parsed_obj = context.parsed_payload.parsed_dict
                    if isinstance(parsed_obj, dict):
                        body_dict = parsed_obj  # type: ignore[assignment]
                elif context.original_body:
                    try:
                        parsed: object = jsonx_loads(context.original_body)
                        if isinstance(parsed, dict):
                            body_dict = parsed  # type: ignore[assignment]
                    except ValueError:
                        pass
                thinking_req = classify_thinking_request(body_dict, context.protocol)
                context.thinking_requirement = thinking_req
            # Record thinking observability trace
            if thinking_req.required:
                _thinking_counter = get_counter()
                await _thinking_counter.increment_requested(
                    client_protocol=thinking_req.client_protocol,
                )
                # Build normalized immutable intent from original client request.
                from eggpool.catalog.capabilities import ThinkingRequestIntent

                _has_history = "reasoning_content" in thinking_req.fields
                _has_new_reasoning = any(
                    f in ("reasoning_effort", "thinking", "thinking_budget")
                    for f in thinking_req.fields
                )
                if thinking_req.reasoning_disabled and not any(
                    f in ("reasoning", "thinking", "thinking_budget")
                    for f in thinking_req.fields
                ):
                    _has_new_reasoning = False
                context.thinking_intent = ThinkingRequestIntent(
                    requested_effort=thinking_req.requested_effort,
                    requested_effort_original=thinking_req.requested_effort,
                    requested_budget_tokens=thinking_req.requested_budget_tokens,
                    request_fields=tuple(thinking_req.fields),
                    has_historical_reasoning_content=_has_history,
                    client_requests_new_reasoning=_has_new_reasoning,
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
                    "provider_control_decision": None,
                    "provider_control_warnings": [],
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

        with _maybe_span(self._dispatch_span_recorder, SPAN_RESERVATION_ESTIMATE):
            if context.estimated_reservation_tokens is not None:
                estimated_tokens = context.estimated_reservation_tokens
            else:
                estimated_tokens = estimate_reservation_tokens(context.original_body)
                context.estimated_reservation_tokens = estimated_tokens

        exclude: set[str] = (
            set(context.attempted_accounts) if context.attempted_accounts else set()
        )
        with _maybe_span(self._dispatch_span_recorder, SPAN_ROUTING_PLAN):
            plan = await self._router.build_routing_plan(
                context.model_id,
                exclude_accounts=exclude if exclude else None,
                provider_id=context.provider_id,
                protocol=context.upstream_protocol,
                transcode_eligibility=(
                    {context.protocol, context.upstream_protocol}
                    if context.transcode_required
                    else None
                ),
                client_protocol=context.protocol,
                thinking_requirement=thinking_req if thinking_req.required else None,
                capability_policy=_capability_policy,
                estimated_tokens=int(estimated_tokens),
                request_surface=getattr(context, "request_surface", "chat_completions"),
            )
        eligible_account_names = plan.eligible_names
        ranked_candidates = plan.ranked_candidates

        # Plan 025: surface quarantine exclusions in the routing
        # trace so the dashboard distinguishes bounded quarantine
        # from circuit-breaker rejections.  Quarantined candidates
        # are not in ``ranked_candidates`` (eligibility already
        # filtered them) so we look at the broader enabled-state
        # set and record a synthetic exclusion for any account
        # currently under active quarantine.  This is purely
        # informational — the eligibility filter has already
        # removed them from the candidate set.
        if not eligible_account_names:
            # Phase 5: distinguish pre-dispatch unavailability
            # from post-retry exhaustion. ``build_routing_plan``
            # already excludes ``context.attempted_accounts``; an
            # empty result on the first attempt means no enabled
            # accounts at all (503). An empty result after at
            # least one attempt means every eligible candidate has
            # been tried in this request (502).
            if thinking_req.required:
                # Record thinking rejection. Phase D: also
                # bump capability-specific counters when the
                # collapsed model status explains the
                # rejection.
                _thinking_counter = get_counter()
                rejected_status = await self._determine_thinking_rejection_status(
                    context=context,
                    thinking_req=thinking_req,
                )
                if rejected_status == "unknown":
                    await _thinking_counter.increment_unknown_capability(
                        client_protocol=thinking_req.client_protocol,
                    )
                elif rejected_status == "unsupported":
                    await _thinking_counter.increment_unsupported_capability(
                        client_protocol=thinking_req.client_protocol,
                    )
                await _thinking_counter.increment_rejected(
                    client_protocol=thinking_req.client_protocol,
                    capability_status="no_eligible_providers",
                )
                if context.thinking_trace is not None:
                    context.thinking_trace["decision"] = "rejected"
                    context.thinking_trace["capability_status"] = (
                        rejected_status or "no_eligible_providers"
                    )
                raise CapabilityError(
                    model_id=context.model_id,
                    capability="thinking",
                    requested_fields=thinking_req.fields,
                    message=(
                        f"Model {context.model_id!r} is available, "
                        f"but no eligible provider is known to "
                        f"support requested thinking controls "
                        f"(thinking capability status: "
                        f"{rejected_status or 'unknown'})."
                    ),
                )
            if self._all_accounts_attempted(
                context, capability_policy=_capability_policy
            ):
                raise UpstreamExhaustedError(
                    f"All eligible accounts attempted for model {context.model_id!r}"
                )
            raise ModelUnavailableError(
                f"No accounts available for model {context.model_id!r}"
            )

        # Phase 5: select candidate using the precomputed plan BEFORE
        # acquiring the lock.  The selection step only reads
        # ``ranked_candidates`` (an in-memory list) and writes
        # ``selected_state`` / ``exclusions`` on this call's stack.
        # The locked region later probes the circuit breaker and
        # claims the account slot atomically; if the breaker
        # rejects the chosen account, the lock-region loop walks
        # down the same plan until it finds an open slot.
        selected_state = None
        selected_score: float | None = None
        selected_tier: int | None = None
        exclusions: list[RoutingExclusion] = []
        claim_receipt = RuntimePublicationReceipt()
        # The first attempt of each request enters the lock; the
        # breaker may have changed state since the plan was built.
        # The locked loop below re-validates the chosen candidate
        # against the live breaker state.

        # --- Phase A: selection claim under _selection_claim_lock #1 ---
        # Probe the circuit breaker and resolve the per-attempt
        # identity.  Database I/O is intentionally excluded so a
        # SQLite waiter cannot convoy other selectors.
        claim_lock_wait_ns = time.perf_counter_ns()
        with _maybe_span(self._dispatch_span_recorder, SPAN_SELECTION_CLAIM_HELD):
            async with self._selection_claim_lock:
                claim_lock_acquired_ns = time.perf_counter_ns()
                claim_lock_wait_delta_ns = claim_lock_acquired_ns - claim_lock_wait_ns
                self._selection_claim_diagnostics.record_claim_lock_wait(
                    claim_lock_wait_delta_ns / 1_000_000
                )
                if self._dispatch_span_recorder is not None:
                    self._dispatch_span_recorder.record_ns(
                        SPAN_SELECTION_CLAIM_WAIT, claim_lock_wait_delta_ns
                    )
                self._selection_claim_diagnostics.record_claim_created()
                # 2. Probe circuit-breaker slots on ranked candidates
                #    (the breaker state is the only mutable runtime
                #    input that depends on the lock).
                with (
                    _maybe_span(
                        self._dispatch_span_recorder,
                        SPAN_SELECTION_REVALIDATION,
                    ),
                    _maybe_span(self._dispatch_span_recorder, SPAN_CIRCUIT_PROBE),
                ):
                    for candidate_state, score in ranked_candidates:
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
                        claim_receipt.health_probe_acquired = (
                            self._health_manager is not None
                        )
                        selected_state = candidate_state
                        selected_score = float(score.final_score)
                        selected_tier = score.tier
                        break

                if selected_state is None:
                    # Distinguish "all enabled accounts already
                    # attempted in this request" (502 UpstreamExhausted)
                    # from "no enabled accounts at all" (503
                    # ModelUnavailable).
                    if thinking_req.required:
                        _thinking_counter = get_counter()
                        rejected_status = (
                            await self._determine_thinking_rejection_status(
                                context=context,
                                thinking_req=thinking_req,
                            )
                        )
                        if rejected_status == "unknown":
                            await _thinking_counter.increment_unknown_capability(
                                client_protocol=thinking_req.client_protocol,
                            )
                        elif rejected_status == "unsupported":
                            _cp = thinking_req.client_protocol
                            await _thinking_counter.increment_unsupported_capability(
                                client_protocol=_cp,
                            )
                        await _thinking_counter.increment_rejected(
                            client_protocol=thinking_req.client_protocol,
                            capability_status="no_eligible_providers",
                        )
                        if context.thinking_trace is not None:
                            context.thinking_trace["decision"] = "rejected"
                            context.thinking_trace["capability_status"] = (
                                rejected_status or "no_eligible_providers"
                            )
                        raise CapabilityError(
                            model_id=context.model_id,
                            capability="thinking",
                            requested_fields=thinking_req.fields,
                            message=(
                                f"Model {context.model_id!r} is available, "
                                f"but no eligible provider is known to "
                                f"support requested thinking controls "
                                f"(thinking capability status: "
                                f"{rejected_status or 'unknown'})."
                            ),
                        )
                    if self._all_accounts_attempted(
                        context, capability_policy=_capability_policy
                    ):
                        raise UpstreamExhaustedError(
                            f"All eligible accounts attempted for model "
                            f"{context.model_id!r}"
                        )
                    raise ModelUnavailableError(
                        f"No accounts available for model {context.model_id!r}"
                    )

                # 3. Resolve API key, account id, provider id, and cost
                #    estimate (all under the lock).
                account_name = selected_state.name
                with _maybe_span(self._dispatch_span_recorder, SPAN_ACCOUNT_LOOKUP):
                    api_key = self._registry.get_api_key(account_name)
                    has_creds = self._registry.has_usable_credentials(account_name)
                    if api_key is None or not has_creds:
                        self._release_unpublished_claim(
                            account_name=account_name,
                            estimated_tokens=estimated_tokens,
                            receipt=claim_receipt,
                        )
                        raise AuthenticationError(
                            f"API key not available for account {account_name!r}"
                        )

                    identity = self._account_identities.get(account_name)
                    account_id = identity.account_id if identity is not None else None
                    if account_id is None:
                        self._release_unpublished_claim(
                            account_name=account_name,
                            estimated_tokens=estimated_tokens,
                            receipt=claim_receipt,
                        )
                        raise DatabaseError(
                            f"Account {account_name!r} not found in database"
                        )

                    resolved_provider_id = (
                        self._catalog.cache.get_provider_for_account(account_name)
                        or self._registry.get_provider_for_account(account_name)
                        or context.provider_id
                        or DEFAULT_PROVIDER_ID
                    )

                    estimated_microdollars = 0
                    if self._quota_estimator is not None:
                        estimated_microdollars = self._quota_estimator.estimate_cost(
                            account_name,
                            context.model_id,
                            estimated_tokens,
                        )

                claim_identity = self._ClaimIdentity(
                    account_name=account_name,
                    account_id=account_id,
                    resolved_provider_id=resolved_provider_id,
                    api_key=api_key,
                    estimated_microdollars=estimated_microdollars,
                )
                if self._quota_estimator is not None:
                    try:
                        self._quota_estimator.add_pending_claim(
                            account_name,
                            tokens=estimated_tokens,
                            cost=estimated_microdollars,
                        )
                    except BaseException:
                        self._release_unpublished_claim(
                            account_name=account_name,
                            estimated_tokens=estimated_tokens,
                            estimated_microdollars=estimated_microdollars,
                            receipt=claim_receipt,
                        )
                        raise
                    claim_receipt.pending_request_added = True
                    claim_receipt.pending_tokens_added = True

        # --- Phase B: durable commit, OUTSIDE the lock ---
        db_request_id: str | None = None
        attempt_id: int | None = None
        reservation_id: str | None = None
        with _maybe_span(
            self._dispatch_span_recorder,
            SPAN_DISPATCH_PERSISTENCE_TRANSACTION,
        ):
            try:
                async with self._db.transaction():
                    with _maybe_span(
                        self._dispatch_span_recorder,
                        SPAN_DISPATCH_PERSISTENCE_COMMIT,
                    ):
                        (
                            db_request_id,
                            reservation_id,
                            attempt_id,
                        ) = await self._persist_dispatch_bundle(
                            context=context,
                            account_id=claim_identity.account_id,
                            resolved_provider_id=claim_identity.resolved_provider_id,
                            estimated_tokens=estimated_tokens,
                            estimated_microdollars=claim_identity.estimated_microdollars,
                            attempt_number=attempt_number,
                        )
            except BaseException:
                # SQLite transaction rolled back; release the health slot
                # the claim phase took, then re-raise.
                self._release_unpublished_claim(
                    account_name=claim_identity.account_name,
                    estimated_tokens=estimated_tokens,
                    estimated_microdollars=claim_identity.estimated_microdollars,
                    receipt=claim_receipt,
                )
                raise

        if (
            not db_request_id
            or not reservation_id
            or not attempt_id
            or isinstance(attempt_id, bool)
            or attempt_id < 1
        ):
            self._release_unpublished_claim(
                account_name=claim_identity.account_name,
                estimated_tokens=estimated_tokens,
                estimated_microdollars=claim_identity.estimated_microdollars,
                receipt=claim_receipt,
            )
            raise DatabaseError(
                "Dispatch persistence returned an invalid durable identity"
            )
        post_commit_selected = SelectedAttempt(
            proxy_request_id=context.request_id,
            db_request_id=db_request_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            account_id=claim_identity.account_id,
            account_name=claim_identity.account_name,
            api_key=claim_identity.api_key,
            model_id=context.model_id,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=claim_identity.estimated_microdollars,
            attempt_number=attempt_number,
            provider_id=claim_identity.resolved_provider_id,
            requires_transcode=context.transcode_required,
            protocol=context.protocol,
            streamed=context.streaming,
        )
        context.client_metadata.update(
            {
                "db_request_id": db_request_id,
                "attempt_id": attempt_id,
                "reservation_id": reservation_id,
                "_post_commit_selected": post_commit_selected,
            }
        )

        # --- Phase C: runtime publication under _selection_claim_lock #2 ---
        publish_lock_wait_ns = time.perf_counter_ns()
        publication_receipt = claim_receipt
        try:
            with _maybe_span(self._dispatch_span_recorder, SPAN_SELECTION_CLAIM_HELD):
                async with self._selection_claim_lock:
                    publish_lock_acquired_ns = time.perf_counter_ns()
                    self._selection_claim_diagnostics.record_claim_lock_wait(
                        (publish_lock_acquired_ns - publish_lock_wait_ns) / 1_000_000
                    )
                    with (
                        _maybe_span(
                            self._dispatch_span_recorder,
                            SPAN_RUNTIME_PUBLICATION,
                        ),
                        _maybe_span(
                            self._dispatch_span_recorder,
                            SPAN_POST_COMMIT_PUBLICATION,
                        ),
                    ):
                        await self._publish_runtime_state(
                            account_name=claim_identity.account_name,
                            estimated_tokens=estimated_tokens,
                            estimated_microdollars=(
                                claim_identity.estimated_microdollars
                            ),
                            receipt=publication_receipt,
                        )
                context.client_metadata["post_commit_published"] = True
                context.attempted_accounts.add(claim_identity.account_name)
                context.client_metadata["account_name"] = claim_identity.account_name
        except BaseException:
            with (
                _maybe_span(self._dispatch_span_recorder, SPAN_CLAIM_ROLLBACK),
                _maybe_span(
                    self._dispatch_span_recorder,
                    SPAN_POST_COMMIT_COMPENSATION,
                ),
            ):
                await self._compensate_or_rollback_claim(
                    context=context,
                    claim_identity=claim_identity,
                    db_request_id=db_request_id,
                    attempt_id=attempt_id,
                    reservation_id=reservation_id,
                    attempt_number=attempt_number,
                    estimated_tokens=estimated_tokens,
                    error=sys.exc_info()[1]
                    or RuntimeError("post_commit_publish_failed"),
                    receipt=publication_receipt,
                )
            raise

        runtime_lease = AttemptRuntimeLease(
            account_name=claim_identity.account_name,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=claim_identity.estimated_microdollars,
            active_count_acquired=publication_receipt.active_count_added,
            quota_reservation_acquired=publication_receipt.quota_reservation_added,
            health_probe_acquired=publication_receipt.health_probe_acquired,
        )
        post_commit_selected = replace(
            post_commit_selected,
            runtime_lease=runtime_lease,
        )
        context.client_metadata["_post_commit_selected"] = post_commit_selected

        # Aliases after the lock releases so the trace-write and
        # SelectedAttempt construction below keep the same variable
        # names the Phase 5 code expected.  Everything flows from
        # ``claim_identity`` + the committed DB ids.
        account_id = claim_identity.account_id
        account_name = claim_identity.account_name
        api_key = claim_identity.api_key
        resolved_provider_id = claim_identity.resolved_provider_id
        estimated_microdollars = claim_identity.estimated_microdollars

        # 10a. Submit the routing-decision trace to the async writer.
        # Trace writes are observability data only; they are not
        # required for reservation correctness, so a trace-write
        # failure cannot fail the dispatch.  The guard acts as a
        # pre-enqueue pressure signal to avoid adding trace pressure
        # while the DB is contended.
        should_write_trace = self._should_write_routing_trace(context.request_id)
        with _maybe_span(self._dispatch_span_recorder, SPAN_ROUTING_TRACE_BUILD):
            trace_cfg = self._config.routing.trace if self._config is not None else None

            trace_event: RoutingTraceEvent | None = None
            if should_write_trace:
                writer_snap = (
                    self._routing_trace_writer.snapshot()
                    if self._routing_trace_writer is not None
                    else None
                )
                skip_trace = False
                skip_reason = "ok"
                if self._routing_trace_guard is not None:
                    skip_trace, skip_reason = self._routing_trace_guard.should_skip(
                        self._db, writer_snap
                    )
                if skip_trace and self._routing_trace_guard is not None:
                    self._routing_trace_guard.record_skip(reason=skip_reason)
                else:
                    top_score_value: float | None = None
                    top_score_account_name: str | None = None
                    if ranked_candidates:
                        top_state, top_score_obj = ranked_candidates[0]
                        top_score_value = float(top_score_obj.final_score)
                        top_score_account_name = top_state.name
                    include_sc = (
                        trace_cfg.include_score_components  # type: ignore[union-attr]
                        if trace_cfg is not None
                        else True
                    )
                    score_components = (
                        self._build_score_components(
                            ranked_candidates=ranked_candidates,
                            selected_account_name=account_name,
                            selected_state=selected_state,
                            selected_score=selected_score,
                            selected_tier=selected_tier,
                            fairness_decision=plan.fairness_decision,
                            fairness_band_names=plan.fairness_band_names,
                        )
                        if include_sc
                        else None
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
                        exclusions=tuple(exclusions) + plan.exclusions,
                        score_components=score_components,
                    )
                    from eggpool.observability.routing_trace_writer import (
                        RoutingTraceEvent,
                    )

                    trace_event = RoutingTraceEvent(
                        request_id=context.request_id,
                        db_request_id=int(db_request_id),
                        attempt_number=attempt_number,
                        model_id=trace.model_id,
                        provider_id=trace.provider_id,
                        protocol=trace.protocol,
                        selected_account_name=trace.selected_account_name,
                        selected_account_id=trace.selected_account_id,
                        selected_tier=trace.selected_tier,
                        selected_score=trace.selected_score,
                        eligible_count=trace.eligible_count,
                        scored_count=trace.scored_count,
                        attempted_excluded_count=trace.attempted_excluded_count,
                        top_score=trace.top_score,
                        top_score_account_name=trace.top_score_account_name,
                        exclude_reasons_json=trace.to_exclude_reasons_json(),
                        score_components_json=(
                            trace.to_score_components_json()
                            if trace.score_components is not None
                            else None
                        ),
                        created_at_mono_ns=time.monotonic_ns(),
                        created_at_epoch=time.time(),
                        generation_id=None,
                    )
        if trace_event is not None and self._routing_trace_writer is not None:
            with _maybe_span(self._dispatch_span_recorder, SPAN_ROUTING_TRACE_WRITE):
                result = self._routing_trace_writer.submit(trace_event)
                if result == "accepted" and self._routing_trace_guard is not None:
                    self._routing_trace_guard.record_written()
                elif self._routing_trace_guard is not None:
                    self._routing_trace_guard.record_skip(reason=result)

        return post_commit_selected

    def _should_write_routing_trace(self, request_id: str) -> bool:
        """Decide trace sampling before constructing any trace details."""
        trace_cfg = self._config.routing.trace if self._config is not None else None
        if trace_cfg is None or trace_cfg.mode == "all":
            return True
        if trace_cfg.mode == "off":
            return False
        bucket = zlib.crc32(request_id.encode("utf-8"))
        return bucket / (2**32) < trace_cfg.sample_rate

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
            if not context.client_metadata.get("_cancelled_finalized"):
                context.client_metadata["_cancelled_finalized"] = True
                elapsed_ms = self._elapsed_ms(context)
                await self._finalize_terminal(
                    context,
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.CLIENT_CANCELLED,
                        error_class="CancelledError",
                        upstream_latency_ms=elapsed_ms,
                        bytes_received=context.original_body_size
                        or len(context.original_body),
                        downstream_started=context.response_handoff.started,
                        upstream_protocol=context.upstream_protocol,
                        thinking_trace_json=_serialize_thinking_trace(
                            context.thinking_trace
                        ),
                        segmentation=context.segmentation,
                        segmentation_not_collected=context.segmentation_not_collected,
                    ),
                )
                self._stream_diagnostics.record_outcome(
                    STREAM_OUTCOME_CLIENT_CANCELLED,
                    proxy_request_id=context.request_id,
                    db_request_id=selected.db_request_id,
                    provider_id=selected.provider_id,
                    account_name=selected.account_name,
                    model_id=selected.model_id,
                    protocol=context.upstream_protocol,
                    elapsed_ms=elapsed_ms,
                    attempt=selected.attempt_number,
                    exception_class="CancelledError",
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
        try:
            headers = self._build_upstream_headers(context, selected)
            upstream_url = self._get_upstream_url(
                context.upstream_protocol,
                selected.provider_id,
                request_surface=getattr(context, "request_surface", "chat_completions"),
            )
            from eggpool.request.transform_pipeline import (
                run_provider_transforms,
            )

            pipeline_result = run_provider_transforms(self, context, selected)
            if pipeline_result.rejection is not None:
                rejection = pipeline_result.rejection
                raise CapabilityError(
                    model_id=str(getattr(context, "model_id", "")),
                    capability="provider_transform",
                    requested_fields=[],
                    message=(
                        "request rejected by provider transform "
                        f"{getattr(rejection, 'category', 'unknown')!r}"
                    ),
                )
        except CapabilityError as err:
            await self._finalize_selected_capability_rejection(
                context=context,
                selected=selected,
                err=err,
            )
            raise
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_preparation",
                error=err,
            ) from err

        # Freeze the exact provider generation that will be dispatched.
        try:
            self._serialize_provider_request(context)
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_serialization",
                error=err,
            ) from err

        # Validate serialized body size against provider limits.
        try:
            self._validate_serialized_request_size(
                context,
                context.body_for_upstream,
                selected_provider_id=selected.provider_id,
            )
        except RequestTooLargeError as err:
            await self._finalize_selected_oversize_rejection(
                context=context,
                selected=selected,
                err=err,
            )
            raise

        try:
            client = self._get_client(selected.provider_id, selected.account_name)
            upstream_request = client.build_request(
                "POST",
                upstream_url,
                headers=headers,
                content=context.body_for_upstream,
            )
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_construction",
                error=err,
            ) from err

        response: httpx.Response | None = None
        try:
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
        except httpx.TransportError as err:
            await self._close_response(response)
            status_code = 504 if isinstance(err, httpx.TimeoutException) else None
            if isinstance(err, httpx.RemoteProtocolError):
                status_code = 502
            raise _RetryableUpstreamError(
                f"Upstream transport failed ({type(err).__name__})",
                status_code=status_code,
                error_class=type(err).__name__,
            ) from err
        except asyncio.CancelledError:
            await self._close_response(response)
            raise
        except Exception as err:
            await self._close_response(response)
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="response_read",
                error=err,
            ) from err

        # Plan 028: single-decode lifecycle via ParsedUpstreamResponse.
        # Created once after aread() completes so both error and success
        # paths share one decoded representation — no redundant parses.
        # Built inside the guarded region below so a parse failure cannot
        # leak the upstream response.
        try:
            resp_headers = filter_response_headers(response.headers)
            from eggpool.request.parsed_upstream_response import (
                build_parsed_upstream_response,
            )

            parsed_response = build_parsed_upstream_response(
                status_code=response.status_code,
                headers=resp_headers,
                raw_body=response.content,
            )

            # Check for upstream errors before consuming body
            if response.status_code >= 400:
                resp_body = response.content

                # Check if this is retryable
                error, failure_observation, failure_effects = (
                    self._classify_upstream_failure(
                        context=context,
                        selected=selected,
                        status_code=response.status_code,
                        headers=resp_headers,
                        body=resp_body,
                    )
                )
                if error is not None:
                    # Retryable error - raise for retry
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=failure_effects.retry_after_s,
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                        retry_category=None,
                        failure_observation=failure_observation,
                        failure_effects=failure_effects,
                    ) from error

                # Non-retryable client error (400, 404) - finalize and pass through
                # Phase 2: re-render upstream error in client protocol.
                # Plan 028: reuse the already-parsed response instead of
                # re-parsing raw bytes via jsonx_loads().
                if transcoder is not None and context.transcode_context is not None:
                    err_payload = parsed_response.parsed_dict
                    if isinstance(err_payload, dict) or err_payload is None:
                        try:
                            _status, err_body, err_warnings = transcoder.reencode_error(
                                response.status_code,
                                err_payload,
                                context.transcode_context,
                            )
                            resp_body = encode_json_body(err_body)
                            context.transcode_context.loss_warnings.extend(err_warnings)
                        except Exception:
                            logger.warning(
                                "Error response adaptation failed; preserving "
                                "filtered upstream body: request_id=%s",
                                context.request_id,
                                exc_info=True,
                            )
                await self._finalize_non_retryable(
                    context,
                    selected,
                    response.status_code,
                    resp_headers,
                    resp_body,
                    failure_observation=failure_observation,
                    failure_effects=failure_effects,
                )
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

            # Success path — Plan 028: reuse the already-parsed response
            # for usage extraction, normalized usage construction, and
            # response transcoding.
            body = parsed_response.raw_body
            elapsed_ms = self._elapsed_ms(context)

            usage = self._extract_non_stream_usage_from_parsed(
                context.upstream_protocol,
                parsed_response,
                provider_id=selected.provider_id,
            )
            normalized_usage = _build_normalized_usage(
                usage=usage,
                raw_payload=parsed_response.parsed_dict,
                protocol=context.upstream_protocol,
                provider_id=selected.provider_id,
                model_id=selected.model_id,
                is_streaming=False,
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
            if transcoder is not None and context.transcode_context is not None:
                upstream_payload = parsed_response.parsed_dict
                if not isinstance(upstream_payload, dict):
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="response_adaptation",
                        error=ValueError(
                            "transcoded success response is not a JSON object"
                        ),
                    )
                try:
                    _features = (
                        self._transcoder_policy.features
                        if self._transcoder_policy is not None
                        else None
                    )
                    translated, decode_warnings = transcoder.decode_response(
                        upstream_payload,
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
                except Exception as err:
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="response_adaptation",
                        error=err,
                    ) from err

            await self._finalize_terminal(
                context,
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
                    bytes_emitted=len(parsed_response.raw_body),
                    upstream_request_id=upstream_req_id,
                    upstream_connect_ms=upstream_connect_ms,
                    upstream_read_ms=upstream_read_ms,
                    coordinator_overhead_ms=coordinator_overhead_ms,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
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
                    normalized_usage=normalized_usage,
                    transcoded=context.transcode_context is not None,
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
                ),
            )

            # Clear persisted backoff rows on a successful request so
            # restart-time hydration starts from a clean slate for this
            # account/model. Authentication remains terminal; matching
            # bounded model quarantine is recoverable on successful traffic.
            await self._clear_backoff(
                selected.account_name,
                model_id=selected.model_id,
                reasons=list(_SUCCESS_CLEAR_BACKOFF_REASONS),
            )

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
                await self._close_response(response)

    async def _execute_streaming(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
        *,
        transcoder: BodyTranscoder | None = None,
    ) -> PreparedProxyResponse:
        """Execute a streaming request."""
        try:
            headers = self._build_upstream_headers(context, selected)
            upstream_url = self._get_upstream_url(
                context.upstream_protocol,
                selected.provider_id,
                request_surface=getattr(context, "request_surface", "chat_completions"),
            )
            from eggpool.request.transform_pipeline import (
                run_provider_transforms,
            )

            pipeline_result = run_provider_transforms(self, context, selected)
            if pipeline_result.rejection is not None:
                rejection = pipeline_result.rejection
                raise CapabilityError(
                    model_id=str(getattr(context, "model_id", "")),
                    capability="provider_transform",
                    requested_fields=[],
                    message=(
                        "request rejected by provider transform "
                        f"{getattr(rejection, 'category', 'unknown')!r}"
                    ),
                )
        except CapabilityError as err:
            await self._finalize_selected_capability_rejection(
                context=context,
                selected=selected,
                err=err,
            )
            raise
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_preparation",
                error=err,
            ) from err
        try:
            self._serialize_provider_request(context)
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_serialization",
                error=err,
            ) from err
        # Validate serialized body size against provider limits.
        try:
            self._validate_serialized_request_size(
                context,
                context.body_for_upstream,
                selected_provider_id=selected.provider_id,
            )
        except RequestTooLargeError as err:
            await self._finalize_selected_oversize_rejection(
                context=context,
                selected=selected,
                err=err,
            )
            raise
        body_to_send = context.body_for_upstream
        upstream_include_usage = context.client_metadata.get("upstream_include_usage")

        try:
            client = self._get_client(selected.provider_id, selected.account_name)
            request = client.build_request(
                "POST",
                upstream_url,
                headers=headers,
                content=body_to_send,
            )
        except Exception as err:
            raise self._local_dispatch_error(
                context=context,
                selected=selected,
                stage="request_construction",
                error=err,
            ) from err

        response = None
        upstream_iterator: AsyncIterator[bytes] | None = None
        generator_created = False
        try:
            try:
                response = await self._send_upstream_request(client, request, context)
            except httpx.TransportError as err:
                if isinstance(err, httpx.ReadTimeout):
                    self._stream_diagnostics.record_outcome(
                        STREAM_OUTCOME_RESPONSE_HEADER_TIMEOUT,
                        proxy_request_id=context.request_id,
                        provider_id=selected.provider_id,
                        account_name=selected.account_name,
                        model_id=selected.model_id,
                        protocol=context.upstream_protocol,
                        elapsed_ms=self._elapsed_ms(context),
                        attempt=selected.attempt_number,
                        exception_class=type(err).__name__,
                    )
                status_code = 504 if isinstance(err, httpx.TimeoutException) else None
                if isinstance(err, httpx.RemoteProtocolError):
                    status_code = 502
                raise _RetryableUpstreamError(
                    f"Upstream transport failed ({type(err).__name__})",
                    status_code=status_code,
                    error_class=type(err).__name__,
                ) from err
            except asyncio.CancelledError:
                raise
            except Exception as err:
                raise self._local_dispatch_error(
                    context=context,
                    selected=selected,
                    stage="response_headers",
                    error=err,
                ) from err

            if response is None:  # type: ignore[reportUnnecessaryComparison]
                raise DatabaseError("Upstream response is None")

            # Check upstream status before creating downstream response
            if response.status_code >= 400:
                try:
                    await response.aread()
                except httpx.TransportError as err:
                    status_code = (
                        504 if isinstance(err, httpx.TimeoutException) else None
                    )
                    raise _RetryableUpstreamError(
                        f"Upstream error-body transport failed ({type(err).__name__})",
                        status_code=status_code,
                        error_class=type(err).__name__,
                    ) from err
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="error_response_read",
                        error=err,
                    ) from err
                try:
                    resp_headers = filter_response_headers(response.headers)
                except Exception as err:
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="error_response_headers",
                        error=err,
                    ) from err
                resp_body = response.content

                error, failure_observation, failure_effects = (
                    self._classify_upstream_failure(
                        context=context,
                        selected=selected,
                        status_code=response.status_code,
                        headers=resp_headers,
                        body=resp_body,
                    )
                )
                if error is not None:
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=failure_effects.retry_after_s,
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                        retry_category=None,
                        failure_observation=failure_observation,
                        failure_effects=failure_effects,
                    ) from error

                # Non-retryable client error - defer the single terminal
                # transition to _handle_exhausted(), which owns the same
                # retained job as every other request-level outcome.
                raise _NonRetryableUpstreamError(
                    f"Upstream returned {response.status_code}",
                    status_code=response.status_code,
                    error_class=failure_effects.evidence_class,
                    failure_observation=failure_observation,
                    failure_effects=failure_effects,
                    upstream_response=(
                        response.status_code,
                        resp_headers,
                        resp_body,
                    ),
                )

            provider_config = (
                self._config.providers.get(selected.provider_id)
                if self._config is not None
                else None
            )
            stream_timeouts = getattr(provider_config, "stream_timeouts", None)
            first_byte_timeout_s = getattr(
                stream_timeouts, "first_byte_timeout_s", None
            )
            idle_timeout_s = getattr(stream_timeouts, "idle_timeout_s", None)
            try:
                upstream_iterator = response.aiter_bytes()
            except Exception as err:
                raise self._local_dispatch_error(
                    context=context,
                    selected=selected,
                    stage="stream_iterator",
                    error=err,
                ) from err
            prefetched_chunk: bytes | None = None
            if first_byte_timeout_s is not None:
                first_byte_started = time.monotonic()
                first_byte_deadline = first_byte_started + first_byte_timeout_s
                try:
                    while True:
                        remaining = first_byte_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        candidate = await asyncio.wait_for(
                            anext(upstream_iterator), timeout=remaining
                        )
                        if candidate:
                            prefetched_chunk = candidate
                            break
                except StopAsyncIteration:
                    pass
                except httpx.TransportError as exc:
                    status_code = (
                        504 if isinstance(exc, httpx.TimeoutException) else None
                    )
                    raise _RetryableUpstreamError(
                        f"Upstream first-byte transport failed ({type(exc).__name__})",
                        status_code=status_code,
                        error_class=type(exc).__name__,
                    ) from exc
                except TimeoutError as exc:
                    timeout = ProviderStreamTimeoutError(
                        STREAM_OUTCOME_FIRST_BYTE_TIMEOUT,
                        timeout_s=first_byte_timeout_s,
                        elapsed_ms=int((time.monotonic() - first_byte_started) * 1000),
                    )
                    self._stream_diagnostics.record_outcome(
                        timeout.outcome,
                        proxy_request_id=context.request_id,
                        provider_id=selected.provider_id,
                        account_name=selected.account_name,
                        model_id=selected.model_id,
                        protocol=context.upstream_protocol,
                        elapsed_ms=timeout.elapsed_ms,
                        attempt=selected.attempt_number,
                        exception_class=type(timeout).__name__,
                        configured_first_byte_timeout_s=first_byte_timeout_s,
                        configured_idle_timeout_s=idle_timeout_s,
                        configured_max_lifetime_s=None,
                    )
                    raise timeout from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise self._local_dispatch_error(
                        context=context,
                        selected=selected,
                        stage="first_byte_prefetch",
                        error=exc,
                    ) from exc

            # Build the response headers
            try:
                resp_headers = filter_response_headers(response.headers)
            except Exception as err:
                raise self._local_dispatch_error(
                    context=context,
                    selected=selected,
                    stage="response_headers",
                    error=err,
                ) from err
            resp_headers.append(("x-proxy-request-id", context.request_id))
            resp_headers.append(("x-proxy-attempt-count", str(attempt_num)))

            # Build streaming generator
            stream_iter = self._build_stream_generator(
                context=context,
                upstream_response=response,
                selected=selected,
                resp_headers=resp_headers,
                request_started_monotonic=context.started_monotonic,
                upstream_include_usage=upstream_include_usage,
                upstream_iterator=upstream_iterator,
                prefetched_chunk=prefetched_chunk,
                stream_first_byte_timeout_s=first_byte_timeout_s,
                stream_idle_timeout_s=idle_timeout_s,
                stream_max_lifetime_s=None,
            )
            generator_created = True
        except ProviderStreamTimeoutError as err:
            _timeout_error, failure_observation, failure_effects = (
                self._classify_upstream_failure(
                    context=context,
                    selected=selected,
                    status_code=504,
                    headers=[],
                    body=None,
                )
            )
            raise _RetryableUpstreamError(
                f"Provider stream timeout: {err.outcome}",
                status_code=504,
                error_class=err.outcome,
                retry_after=failure_effects.retry_after_s,
                upstream_response=None,
                retry_category=None,
                failure_observation=failure_observation,
                failure_effects=failure_effects,
            ) from err
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
                if upstream_iterator is not None and not generator_created:
                    close_iterator = getattr(upstream_iterator, "aclose", None)
                    if close_iterator is not None:
                        try:
                            await close_iterator()
                        except Exception:
                            logger.debug(
                                "Error closing upstream iterator", exc_info=True
                            )
                try:
                    await response.aclose()
                except Exception:
                    logger.debug("Error closing upstream response", exc_info=True)

        if response is None:
            raise DatabaseError("Upstream response is None")

        # The chosen response is prepared and no retry or response adapter
        # needs the request body after this handoff boundary.
        context.release_dispatch_buffers()
        return PreparedProxyResponse(
            status_code=response.status_code,
            headers=resp_headers,
            stream_iterator=stream_iter,
            request_id=context.request_id,
            account_name=selected.account_name,
            latency_ms=self._elapsed_ms(context),
            attempt_count=attempt_num,
            response_handoff=context.response_handoff,
        )

    def _build_stream_generator(
        self,
        context: ProxyRequestContext,
        upstream_response: httpx.Response,
        selected: SelectedAttempt,
        resp_headers: list[tuple[str, str]],
        request_started_monotonic: float | None = None,
        upstream_include_usage: bool | None = None,
        upstream_iterator: AsyncIterator[bytes] | None = None,
        prefetched_chunk: bytes | None = None,
        stream_first_byte_timeout_s: float | None = None,
        stream_idle_timeout_s: float | None = None,
        stream_max_lifetime_s: float | None = None,
    ) -> AsyncIterator[bytes]:
        """Build an async generator that streams upstream bytes downstream,
        extracts usage via IncrementalSSEObserver, and finalizes the request
        on completion.

        ``upstream_include_usage`` is computed once in ``_execute_streaming``
        from the OpenAI ``stream_options.include_usage`` field after any
        injection, then passed in to avoid re-parsing the body here.  When
        ``None`` (Anthropic upstreams, or the upstream protocol did not
        expose ``stream_options``) the default ``True`` is used so
        existing behaviour is preserved.
        """
        observer = IncrementalSSEObserver(
            context.upstream_protocol,
            provider_id=selected.provider_id,
            request_surface=getattr(context, "request_surface", "chat_completions"),
        )
        shared_decoder = SSEDecoder()
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
        persist_error_detail = self._persist_error_detail
        account_backoff_repo = self._account_backoff_repo
        clear_backoff = self._clear_backoff
        include_usage = (
            True if upstream_include_usage is None else upstream_include_usage
        )

        async def _stream() -> AsyncIterator[bytes]:
            nonlocal bytes_emitted, first_byte_ms
            try:
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
                iterator = upstream_iterator or upstream_response.aiter_bytes()
                pending_chunk = prefetched_chunk
                stream_started_at = time.monotonic()
                last_payload_at = stream_started_at
                while True:
                    if pending_chunk is not None:
                        chunk = pending_chunk
                        pending_chunk = None
                    else:
                        timeout_s = stream_idle_timeout_s
                        timeout_outcome = STREAM_OUTCOME_IDLE_TIMEOUT
                        try:
                            if timeout_s is None:
                                chunk = await anext(iterator)
                            else:
                                chunk = await asyncio.wait_for(
                                    anext(iterator), timeout=timeout_s
                                )
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            now = time.monotonic()
                            timeout = ProviderStreamTimeoutError(
                                timeout_outcome,
                                timeout_s=timeout_s or 0.0,
                                elapsed_ms=int((now - reference) * 1000),
                                idle_ms=int((now - last_payload_at) * 1000),
                            )
                            raise timeout from exc
                    if not chunk:
                        continue
                    last_payload_at = time.monotonic()
                    if first_byte_ms == 0.0:
                        first_byte_ms = (time.monotonic() - reference) * 1000

                    observer.observe_bytes(chunk)
                    bytes_emitted = observer.bytes_emitted
                    frames = shared_decoder.feed(chunk)
                    for frame in frames:
                        observer.observe_frame(frame)

                    if streaming_transcoder is not None:
                        out_chunks: list[bytes] = []
                        for frame in frames:
                            try:
                                out_chunks.extend(
                                    streaming_transcoder.translate_frame(frame)
                                )
                            except Exception as err:
                                raise _LocalStreamTranslationError(str(err)) from err
                        if out_chunks:
                            yield b"".join(out_chunks)
                    else:
                        yield chunk

                # Transport EOF is not protocol completion. Drain the parser,
                # classify first, and only then permit a transcoder flush.
                eof_result = shared_decoder.finish()
                observer.finish(eof_result)
                provider_config = (
                    self._config.providers.get(selected.provider_id)
                    if self._config is not None
                    else None
                )
                completion_policy = cast(
                    "CompletionPolicy",
                    getattr(provider_config, "stream_completion_policy", "strict"),
                )
                if (
                    "text/event-stream"
                    not in upstream_response.headers.get("content-type", "").lower()
                ):
                    # A few OpenAI-compatible providers return a complete JSON
                    # body despite a streaming request. Preserve that legacy
                    # pass-through behavior; SSE completion rules apply to
                    # event-stream responses only.
                    eof_decision = StreamEOFDecision(
                        classification="complete",
                        downstream_started=context.response_handoff.started,
                    )
                else:
                    eof_decision = classify_stream_eof(
                        protocol=context.upstream_protocol,
                        policy=completion_policy,
                        snapshot=observer.completion_snapshot,
                        downstream_started=context.response_handoff.started,
                    )
                if eof_decision.classification not in {
                    "complete",
                    "compatibility_eof",
                }:
                    # Plan 144 (E3): terminal_failure and
                    # terminal_incomplete are Responses-level provider
                    # terminal events that are *not* successful.  They
                    # should not trigger PrematureStreamEOFError (which
                    # is for transport-level early EOFs) and should not
                    # be retried after downstream handoff.
                    if eof_decision.classification == "terminal_failure":
                        eof_outcome = STREAM_OUTCOME_TERMINAL_FAILURE
                    elif eof_decision.classification == "terminal_incomplete":
                        eof_outcome = STREAM_OUTCOME_TERMINAL_INCOMPLETE
                    elif eof_decision.classification == "empty_eof":
                        eof_outcome = STREAM_OUTCOME_EMPTY_EOF
                    elif eof_decision.classification == "malformed_eof":
                        eof_outcome = STREAM_OUTCOME_MALFORMED_EOF
                    elif eof_decision.classification == "premature_eof":
                        eof_outcome = (
                            STREAM_OUTCOME_PREMATURE_EOF_MIDSTREAM
                            if eof_decision.downstream_started
                            else STREAM_OUTCOME_PREMATURE_EOF_BEFORE_BODY
                        )
                    else:
                        # Unreachable for current literal; kept for safety.
                        eof_outcome = STREAM_OUTCOME_MALFORMED_EOF
                    usage_result = observer.usage
                    incomplete_latency = int((time.monotonic() - reference) * 1000)
                    self._stream_diagnostics.record_outcome(
                        eof_outcome,
                        proxy_request_id=context.request_id,
                        db_request_id=selected.db_request_id,
                        provider_id=selected.provider_id,
                        account_name=selected.account_name,
                        model_id=selected.model_id,
                        protocol=context.upstream_protocol,
                        elapsed_ms=incomplete_latency,
                        bytes_emitted=bytes_emitted,
                        first_byte_ms=(
                            int(first_byte_ms) if first_byte_ms > 0 else None
                        ),
                        attempt=selected.attempt_number,
                    )
                    # Plan 144 (E3): terminal_failure and
                    # terminal_incomplete are Responses-level provider
                    # terminal events.  They are not transport-level early
                    # EOFs, so we do not raise PrematureStreamEOFError and
                    # do not trigger provider/account failover after
                    # downstream handoff.  The upstream SSE event has
                    # already been forwarded to the client.
                    if eof_decision.classification in {
                        "terminal_failure",
                        "terminal_incomplete",
                    }:
                        error_class = "ResponsesTerminalEvent"
                    else:
                        error_class = "PrematureStreamEOFError"
                    await self._finalize_terminal(
                        context,
                        selected,
                        FinalizationData(
                            outcome=FinalizationOutcome.MIDSTREAM_ERROR,
                            downstream_started=eof_decision.downstream_started,
                            first_byte_ms=(
                                int(first_byte_ms) if first_byte_ms > 0 else None
                            ),
                            upstream_latency_ms=incomplete_latency,
                            bytes_emitted=bytes_emitted,
                            input_tokens=usage_result.input_tokens,
                            output_tokens=usage_result.output_tokens,
                            cache_read_tokens=usage_result.cache_read_tokens,
                            cache_write_tokens=usage_result.cache_creation_tokens,
                            reasoning_tokens=usage_result.reasoning_tokens,
                            thinking_characters=usage_result.thinking_characters,
                            error_class=error_class,
                            error_detail=eof_decision.classification,
                            bytes_received=context.original_body_size
                            or len(context.original_body),
                            upstream_protocol=context.upstream_protocol,
                            normalized_usage=_build_normalized_usage(
                                usage=usage_result,
                                raw_payload=None,
                                protocol=context.upstream_protocol,
                                provider_id=selected.provider_id,
                                model_id=selected.model_id,
                                is_streaming=True,
                            ),
                            transcoded=context.transcode_context is not None,
                        ),
                    )
                    # The specific ``eof_outcome`` recorded above is the
                    # canonical diagnostic for EOF-classified streams.
                    # Also recording the generic midstream-error rollup
                    # here would double-count every EOF and would count
                    # Responses-level terminal events as transport
                    # errors; the rollup stays reserved for genuine
                    # midstream exceptions on the exception path below.
                    # Plan 144 (E3): terminal_failure/terminal_incomplete
                    # have already been forwarded to the client.  Do not
                    # raise PrematureStreamEOFError — that would trigger
                    # upstream error classification and potential retry,
                    # but the terminal event is the client-visible outcome.
                    #
                    # Plan 145 (Workstream A): ``response.failed`` and
                    # ``response.incomplete`` are provider-level terminal
                    # events that have already been durably finalized as
                    # MIDSTREAM_ERROR.  Returning immediately prevents
                    # fallthrough into the success path, which would
                    # submit a conflicting COMPLETED terminal outcome to
                    # the finalization supervisor (raising
                    # ``TerminalConflictError``) and emit a misleading
                    # ``STREAM_OUTCOME_COMPLETED_*`` diagnostic.
                    if eof_decision.classification in {
                        "terminal_failure",
                        "terminal_incomplete",
                    }:
                        return
                    raise PrematureStreamEOFError(
                        eof_decision.classification,
                        request_id=context.request_id,
                    )
                if streaming_transcoder is not None:
                    try:
                        out_chunks = streaming_transcoder.finish(eof_result)
                    except Exception as err:
                        raise _LocalStreamTranslationError(str(err)) from err
                    if out_chunks:
                        yield b"".join(out_chunks)
                usage_result = observer.usage

                completion_outcome = (
                    STREAM_OUTCOME_COMPLETED_COMPATIBILITY
                    if eof_decision.classification == "compatibility_eof"
                    else STREAM_OUTCOME_COMPLETED_CANONICAL
                )
                self._stream_diagnostics.record_outcome(
                    completion_outcome,
                    proxy_request_id=context.request_id,
                    db_request_id=selected.db_request_id,
                    provider_id=selected.provider_id,
                    account_name=selected.account_name,
                    model_id=selected.model_id,
                    protocol=context.upstream_protocol,
                    bytes_emitted=bytes_emitted,
                    attempt=selected.attempt_number,
                    configured_first_byte_timeout_s=(stream_first_byte_timeout_s),
                    configured_idle_timeout_s=stream_idle_timeout_s,
                    configured_max_lifetime_s=stream_max_lifetime_s,
                )

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

                normalized_usage = _build_normalized_usage(
                    usage=usage_result,
                    raw_payload=None,
                    protocol=context.upstream_protocol,
                    provider_id=selected.provider_id,
                    model_id=selected.model_id,
                    is_streaming=True,
                )

                # Finalize via RequestFinalizer
                await self._finalize_terminal(
                    context,
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.COMPLETED,
                        downstream_started=context.response_handoff.started,
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
                        bytes_received=context.original_body_size
                        or len(context.original_body),
                        upstream_connect_ms=upstream_connect_ms_value,
                        upstream_read_ms=upstream_read_ms_value,
                        coordinator_overhead_ms=coordinator_overhead_ms_value,
                        provider_cost_microdollars=usage_result.reported_cost_microdollars,
                        provider_cost_source=usage_result.reported_cost_source,
                        upstream_protocol=context.upstream_protocol,
                        thinking_trace_json=_serialize_thinking_trace(
                            context.thinking_trace
                        ),
                        normalized_usage=normalized_usage,
                        segmentation=context.segmentation,
                        segmentation_not_collected=context.segmentation_not_collected,
                        transcoded=context.transcode_context is not None,
                    ),
                )

                # Clear matching persisted transient/model backoff rows on a
                # successful streaming request. Authentication remains
                # terminal and local quota estimates are never persisted.
                if account_backoff_repo is not None:
                    await clear_backoff(
                        selected.account_name,
                        model_id=selected.model_id,
                        reasons=list(_SUCCESS_CLEAR_BACKOFF_REASONS),
                    )

                self._stream_diagnostics.record_outcome(
                    STREAM_OUTCOME_COMPLETED,
                    proxy_request_id=context.request_id,
                    db_request_id=selected.db_request_id,
                    provider_id=selected.provider_id,
                    account_name=selected.account_name,
                    model_id=selected.model_id,
                    protocol=context.upstream_protocol,
                    elapsed_ms=upstream_latency_total,
                    bytes_emitted=bytes_emitted,
                    first_byte_ms=(int(first_byte_ms) if first_byte_ms > 0 else None),
                    upstream_connect_ms=upstream_connect_ms_value,
                    upstream_header_ms=self._upstream_header_ms(context),
                    upstream_read_ms=upstream_read_ms_value,
                    attempt=selected.attempt_number,
                    configured_first_byte_timeout_s=(stream_first_byte_timeout_s),
                    configured_idle_timeout_s=stream_idle_timeout_s,
                    configured_max_lifetime_s=stream_max_lifetime_s,
                )

            except asyncio.CancelledError:
                # Client cancellation - finalize but don't penalize health.
                # Skip if _execute_upstream already finalized (the CancelledError
                # propagates here after the outer handler runs).
                #
                # Plan 026/080: when the generation-owned finalization
                # supervisor is
                # available, the retained finalization job owns cleanup even
                # when every request waiter is cancelled.  The job was
                # registered before the inner generator, so it exists in the
                # supervisor's registry regardless of cancellation timing.
                # ``fin_job.run()`` uses ``asyncio.shield`` internally; the
                # retained task continues after the caller is cancelled.
                observer.finish(shared_decoder.finish())
                usage_result = observer.usage
                if not context.client_metadata.get("_cancelled_finalized"):
                    context.client_metadata["_cancelled_finalized"] = True
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
                    fin_data = FinalizationData(
                        outcome=FinalizationOutcome.CLIENT_CANCELLED,
                        downstream_started=context.response_handoff.started,
                        first_byte_ms=(
                            int(first_byte_ms) if first_byte_ms > 0 else None
                        ),
                        upstream_latency_ms=cancel_latency_total,
                        bytes_emitted=bytes_emitted,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=(usage_result.cache_creation_tokens),
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=(usage_result.thinking_characters),
                        bytes_received=context.original_body_size
                        or len(context.original_body),
                        upstream_connect_ms=cancel_connect_ms_value,
                        upstream_read_ms=cancel_read_ms_value,
                        coordinator_overhead_ms=cancel_overhead_ms_value,
                        provider_cost_microdollars=(
                            usage_result.reported_cost_microdollars
                        ),
                        provider_cost_source=(usage_result.reported_cost_source),
                        upstream_protocol=context.upstream_protocol,
                        thinking_trace_json=_serialize_thinking_trace(
                            context.thinking_trace
                        ),
                        normalized_usage=_build_normalized_usage(
                            usage=usage_result,
                            raw_payload=None,
                            protocol=context.upstream_protocol,
                            provider_id=selected.provider_id,
                            model_id=selected.model_id,
                            is_streaming=True,
                        ),
                        transcoded=(context.transcode_context is not None),
                        segmentation=context.segmentation,
                        segmentation_not_collected=context.segmentation_not_collected,
                    )
                    await self._finalize_terminal(context, selected, fin_data)
                    self._stream_diagnostics.record_outcome(
                        STREAM_OUTCOME_CLIENT_CANCELLED,
                        proxy_request_id=context.request_id,
                        db_request_id=selected.db_request_id,
                        provider_id=selected.provider_id,
                        account_name=selected.account_name,
                        model_id=selected.model_id,
                        protocol=context.upstream_protocol,
                        elapsed_ms=cancel_latency_total,
                        bytes_emitted=bytes_emitted,
                        first_byte_ms=(
                            int(first_byte_ms) if first_byte_ms > 0 else None
                        ),
                        upstream_connect_ms=cancel_connect_ms_value,
                        upstream_header_ms=self._upstream_header_ms(context),
                        upstream_read_ms=cancel_read_ms_value,
                        attempt=selected.attempt_number,
                    )
                raise
            except PrematureStreamEOFError:
                raise
            except Exception as exc:
                # Midstream error - finalize, no retry
                observer.finish(shared_decoder.finish())
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
                downstream_started = context.response_handoff.started
                failure_source = (
                    "transcoding"
                    if isinstance(exc, _LocalStreamTranslationError)
                    else "stream"
                )
                failure_observation = self._build_failure_observation(
                    context=context,
                    selected=selected,
                    status_code=(
                        504 if isinstance(exc, ProviderStreamTimeoutError) else None
                    ),
                    error_class=type(exc).__name__,
                    source=failure_source,
                    response_started=downstream_started,
                    downstream_started=downstream_started,
                )
                if isinstance(exc, ProviderStreamTimeoutError):
                    from eggpool.failure.signal import FailureSignal

                    failure_observation = replace(
                        failure_observation,
                        response_signal=FailureSignal.TRANSPORT_FAILURE,
                    )
                failure_effects = classify_failure_effects(failure_observation)
                await self._finalize_terminal(
                    context,
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.MIDSTREAM_ERROR,
                        downstream_started=context.response_handoff.started,
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
                        bytes_received=context.original_body_size
                        or len(context.original_body),
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
                        normalized_usage=_build_normalized_usage(
                            usage=usage_result,
                            raw_payload=None,
                            protocol=context.upstream_protocol,
                            provider_id=selected.provider_id,
                            model_id=selected.model_id,
                            is_streaming=True,
                        ),
                        failure_observation=failure_observation,
                        failure_effects=failure_effects,
                        transcoded=context.transcode_context is not None,
                        segmentation=context.segmentation,
                        segmentation_not_collected=context.segmentation_not_collected,
                    ),
                )
                self._stream_diagnostics.record_outcome(
                    STREAM_OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
                    proxy_request_id=context.request_id,
                    db_request_id=selected.db_request_id,
                    provider_id=selected.provider_id,
                    account_name=selected.account_name,
                    model_id=selected.model_id,
                    protocol=context.upstream_protocol,
                    elapsed_ms=mid_latency_total,
                    bytes_emitted=bytes_emitted,
                    first_byte_ms=(int(first_byte_ms) if first_byte_ms > 0 else None),
                    upstream_connect_ms=mid_connect_ms_value,
                    upstream_header_ms=self._upstream_header_ms(context),
                    upstream_read_ms=mid_read_ms_value,
                    attempt=selected.attempt_number,
                    exception_class=type(exc).__name__,
                )
                # Record the first-class HTTPX transport outcome when
                # the exception class maps to a known upstream label.
                if exc_class_name := type(exc).__name__:
                    if isinstance(exc, ProviderStreamTimeoutError):
                        self._stream_diagnostics.record_outcome(
                            exc.outcome,
                            proxy_request_id=context.request_id,
                            db_request_id=selected.db_request_id,
                            provider_id=selected.provider_id,
                            account_name=selected.account_name,
                            model_id=selected.model_id,
                            protocol=context.upstream_protocol,
                            elapsed_ms=mid_latency_total,
                            bytes_emitted=bytes_emitted,
                            first_byte_ms=(
                                int(first_byte_ms) if first_byte_ms > 0 else None
                            ),
                            idle_ms=exc.idle_ms,
                            attempt=selected.attempt_number,
                            exception_class=exc_class_name,
                            configured_idle_timeout_s=stream_idle_timeout_s,
                            configured_first_byte_timeout_s=(
                                stream_first_byte_timeout_s
                            ),
                            configured_max_lifetime_s=stream_max_lifetime_s,
                        )
                    if not isinstance(exc, ProviderStreamTimeoutError):
                        first_class_outcome = classify_httpx_error_class(exc_class_name)
                        self._stream_diagnostics.record_outcome(
                            first_class_outcome,
                            proxy_request_id=context.request_id,
                            db_request_id=selected.db_request_id,
                            provider_id=selected.provider_id,
                            account_name=selected.account_name,
                            model_id=selected.model_id,
                            protocol=context.upstream_protocol,
                            elapsed_ms=mid_latency_total,
                            bytes_emitted=bytes_emitted,
                            first_byte_ms=(
                                int(first_byte_ms) if first_byte_ms > 0 else None
                            ),
                            upstream_connect_ms=mid_connect_ms_value,
                            upstream_header_ms=self._upstream_header_ms(context),
                            upstream_read_ms=mid_read_ms_value,
                            attempt=selected.attempt_number,
                            exception_class=type(exc).__name__,
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

        Delegates to :func:`usage_helpers.extract_non_stream_usage`.
        """
        from eggpool.request.usage_helpers import extract_non_stream_usage

        return extract_non_stream_usage(protocol, body, provider_id=provider_id)

    def _extract_non_stream_usage_from_parsed(
        self,
        protocol: str,
        parsed: Any,  # ParsedUpstreamResponse
        *,
        provider_id: str | None = None,
    ) -> StreamUsageResult | None:
        """Extract usage from an already-parsed upstream response.

        Delegates to :func:`usage_helpers.extract_non_stream_usage_from_parsed`.
        """
        from eggpool.request.usage_helpers import extract_non_stream_usage_from_parsed

        return extract_non_stream_usage_from_parsed(
            protocol, parsed, provider_id=provider_id
        )

    @staticmethod
    def _get_header_value(
        headers: list[tuple[str, str]],
        name: str | list[str],
    ) -> str | None:
        """Return the value for a header, or None.

        Delegates to :func:`static_helpers.get_header_value`.
        """
        from eggpool.request.static_helpers import get_header_value

        return get_header_value(headers, name)

    @staticmethod
    def _elapsed_ms(context: ProxyRequestContext) -> int:
        """Return request latency from a clock unaffected by wall-clock jumps.

        Delegates to :func:`static_helpers.elapsed_ms`.
        """
        from eggpool.request.static_helpers import elapsed_ms

        return elapsed_ms(context)

    async def _send_upstream_request(
        self,
        client: httpx.AsyncClient,
        request: httpx.Request,
        context: ProxyRequestContext,
    ) -> httpx.Response:
        """Send an upstream request and capture shared dispatch timing.

        Delegates to :func:`upstream_execution.send_upstream_request`.
        """
        from eggpool.request.upstream_execution import send_upstream_request

        return await send_upstream_request(
            client,
            request,
            context,
            local_pre_upstream_recorder=self._local_pre_upstream_recorder,
            dispatch_overhead_recorder=self._dispatch_overhead_recorder,
        )

    @staticmethod
    async def _close_response(response: httpx.Response | None) -> None:
        """Close an upstream response without masking the original failure.

        Delegates to :func:`static_helpers.close_response`."""
        from eggpool.request.static_helpers import close_response

        await close_response(response)

    def _local_dispatch_error(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        stage: str,
        error: Exception,
    ) -> _LocalDispatchError:
        """Build a request-local error with zero provider effects."""
        observation = self._build_failure_observation(
            context=context,
            selected=selected,
            status_code=500,
            error_class=type(error).__name__,
            source="local_preparation",
            response_started=False,
            downstream_started=False,
        )
        return _LocalDispatchError(
            stage=stage,
            error_class=type(error).__name__,
            failure_observation=observation,
            failure_effects=classify_failure_effects(observation),
        )

    async def _finalize_unexpected_local_error(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        error: Exception,
    ) -> None:
        """Submit terminal cleanup for an exception outside a stage helper."""
        local_error = self._local_dispatch_error(
            context=context,
            selected=selected,
            stage="request_boundary",
            error=error,
        )
        await self._finalize_terminal(
            context,
            selected,
            FinalizationData(
                # Local boundary failures have zero provider effects;
                # persisting them as upstream errors would pollute
                # account runtime state and error-rate stats.
                outcome=FinalizationOutcome.CLIENT_ERROR,
                status_code=500,
                error_class=local_error.error_class,
                error_detail="local request failure",
                upstream_latency_ms=self._elapsed_ms(context),
                upstream_protocol=context.upstream_protocol,
                failure_observation=local_error.failure_observation,
                failure_effects=local_error.failure_effects,
                thinking_trace_json=_serialize_thinking_trace(context.thinking_trace),
            ),
        )

    async def _schedule_unexpected_local_cleanup(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        error: Exception,
    ) -> None:
        """Retain bounded attempt cleanup when terminalization itself fails."""
        local_error = self._local_dispatch_error(
            context=context,
            selected=selected,
            stage="request_boundary_cleanup",
            error=error,
        )
        cleanup_error = _RetryableUpstreamError(
            "unexpected local cleanup",
            status_code=500,
            error_class=local_error.error_class,
            failure_observation=local_error.failure_observation,
            failure_effects=local_error.failure_effects,
            source="local_preparation",
        )
        await self._cleanup_failed_attempt(
            context=context,
            selected=selected,
            error=cleanup_error,
        )

    @staticmethod
    def _build_local_error_response(
        context: ProxyRequestContext,
        *,
        status_code: int,
    ) -> PreparedProxyResponse:
        """Build a bounded protocol-shaped local error without exception text.

        Delegates to :func:`static_helpers.build_local_error_response`.
        """
        from eggpool.request.static_helpers import build_local_error_response

        return build_local_error_response(context, status_code=status_code)

    @staticmethod
    def _upstream_read_ms(
        context: ProxyRequestContext,
        observed_elapsed_ms: int,
    ) -> int | None:
        """Return elapsed upstream body/stream read time after response headers.

        Delegates to :func:`static_helpers.upstream_read_ms`.
        """
        from eggpool.request.static_helpers import upstream_read_ms

        return upstream_read_ms(context, observed_elapsed_ms)

    @staticmethod
    def _upstream_header_ms(context: ProxyRequestContext) -> int | None:
        """Return elapsed time to receive upstream response headers.

        Delegates to :func:`static_helpers.upstream_header_ms`.
        """
        from eggpool.request.static_helpers import upstream_header_ms

        return upstream_header_ms(context)

    @staticmethod
    def _coordinator_overhead_ms(
        *,
        total_ms: int,
        connect_ms: int | None,
        read_ms: int | None,
    ) -> int | None:
        """Return elapsed time not attributed to upstream connect or read phases.

        Delegates to :func:`static_helpers.coordinator_overhead_ms`.
        """
        from eggpool.request.static_helpers import coordinator_overhead_ms

        return coordinator_overhead_ms(
            total_ms=total_ms, connect_ms=connect_ms, read_ms=read_ms
        )

    def _build_failure_observation(
        self,
        *,
        context: ProxyRequestContext | None,
        selected: SelectedAttempt | None,
        status_code: int | None,
        headers: list[tuple[str, str]] | None = None,
        body: bytes | None = None,
        error_class: str | None = None,
        source: str = "upstream_http",
        response_started: bool = False,
        downstream_started: bool = False,
    ) -> FailureObservation:
        """Normalize one upstream failure without retaining raw wire data.

        Delegates to :func:`failure_helpers.build_failure_observation`.
        """
        from eggpool.request.failure_helpers import build_failure_observation

        return build_failure_observation(
            context=context,
            selected=selected,
            status_code=status_code,
            headers=headers,
            body=body,
            error_class=error_class,
            source=source,
            response_started=response_started,
            downstream_started=downstream_started,
        )

    @staticmethod
    def _error_from_failure_effects(
        effects: FailureEffects,
        *,
        status_code: int | None,
    ) -> UpstreamError | None:
        """Adapt the canonical decision to the public upstream errors.

        Delegates to :func:`failure_helpers.error_from_failure_effects`.
        """
        from eggpool.request.failure_helpers import error_from_failure_effects

        return error_from_failure_effects(effects, status_code=status_code)

    def _classify_upstream_failure(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        status_code: int,
        headers: list[tuple[str, str]],
        body: bytes | None,
    ) -> tuple[UpstreamError | None, FailureObservation, FailureEffects]:
        """Classify an upstream response once for retry and shared effects.

        Delegates to :func:`failure_helpers.classify_upstream_failure`.
        """
        from eggpool.request.failure_helpers import classify_upstream_failure

        return classify_upstream_failure(
            context=context,
            selected=selected,
            status_code=status_code,
            headers=headers,
            body=body,
        )

    def _classify_upstream_error(
        self,
        status_code: int,
        headers: list[tuple[str, str]],
        body: bytes | None = None,
    ) -> UpstreamError | None:
        """Classify an upstream error status code into an exception.

        Delegates to :func:`failure_helpers.classify_upstream_error`.
        """
        from eggpool.request.failure_helpers import classify_upstream_error

        return classify_upstream_error(status_code, headers, body)

    def _get_upstream_url(
        self,
        protocol: str,
        provider_id: str | None = None,
        *,
        request_surface: str = "chat_completions",
    ) -> str:
        """Get the absolute upstream URL for a protocol and provider.

        ``request_surface`` selects the OpenAI-family endpoint. Defaults
        to ``"chat_completions"`` so the historical dispatch path is
        preserved for callers that do not opt in to the Responses surface.
        Delegates to :func:`upstream_helpers.get_upstream_url`.
        """
        from eggpool.request.upstream_helpers import get_upstream_url

        return get_upstream_url(
            protocol,
            provider_id,
            config=self._config,
            request_surface=request_surface,
        )

    def _resolve_selected_thinking_capability(
        self,
        *,
        model_id: str,
        provider_id: str,
    ) -> ThinkingCapability:
        """Best-effort lookup of the selected provider's thinking capability.

        Delegates to :func:`thinking_adaptation.resolve_selected_thinking_capability`.
        """
        from eggpool.request.thinking_adaptation import (
            resolve_selected_thinking_capability,
        )

        return resolve_selected_thinking_capability(
            self._catalog, model_id, provider_id
        )

    async def _determine_thinking_rejection_status(
        self,
        *,
        context: ProxyRequestContext,
        thinking_req: ThinkingRequestRequirement,
    ) -> str | None:
        """Inspect the collapsed capability to attribute a thinking rejection.

        Returns ``"unknown"`` or ``"unsupported"`` when the collapsed
        model's thinking status matches the rejection reason. Returns
        ``None`` when the status cannot be determined (caller falls
        back to the generic ``no_eligible_providers`` reason).
        """
        from eggpool.catalog.capabilities import extract_thinking_status_from_entry

        try:
            collapsed_entry = self._catalog.cache.get_model(context.model_id)
            status = extract_thinking_status_from_entry(collapsed_entry)
        except Exception:  # noqa: BLE001
            return None
        if status in ("unknown", "unsupported"):
            return status
        # ``thinking_req`` is currently unused; reserved for future
        # per-request overrides that may classify differently.
        del thinking_req
        return None

    def _recompute_thinking_budget_for_selected_provider(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        thinking_capability: ThinkingCapability,
        request: Any,
    ) -> None:
        """Re-resolve ``thinking.budget_tokens`` for the selected provider.

        Delegates to :func:`thinking_adaptation.recompute_thinking_budget_for_provider`.
        """
        from eggpool.request.thinking_adaptation import (
            recompute_thinking_budget_for_provider,
        )

        recompute_thinking_budget_for_provider(
            context=context,
            selected=selected,
            thinking_capability=thinking_capability,
            request=request,
            transcoder_policy=self._transcoder_policy,
        )

    def _apply_selected_provider_transcode_adjustments(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        request: Any | None = None,
    ) -> bool:
        """Apply provider-specific thinking control normalization before dispatch.

        Runs two stages after provider selection:

        1. **Budget recompute** (existing): re-resolves ``thinking.budget_tokens``
           against the selected provider's capability using original client intent.
        2. **Provider control normalization** (Plan 024): validates and adapts
           thinking controls against the selected provider's control contract.

        Both stages run for native and transcoded paths.  Strict-policy
        rejections propagate as :class:`CapabilityError`; callers MUST
        wrap this method with
        :meth:`_finalize_selected_capability_rejection` so durable
        attempt state is cleaned up before the error is re-raised.
        """
        if not selected.provider_id:
            return False
        legacy_request = request is None and context.provider_bound is None
        if request is None:
            request = context.provider_bound
        if request is None:
            request = self._legacy_provider_request(context)
        generation_before = request.payload_generation
        thinking_capability = self._resolve_selected_thinking_capability(
            model_id=context.model_id,
            provider_id=selected.provider_id,
        )
        # Stage 1: budget recompute (only for transcoded paths).
        if context.transcode_required:
            self._recompute_thinking_budget_for_selected_provider(
                context=context,
                selected=selected,
                thinking_capability=thinking_capability,
                request=request,
            )
        # Stage 2: provider control normalization (always runs when
        # thinking controls are present, regardless of transcode_required).
        self._adapt_provider_thinking_controls(
            context=context,
            selected=selected,
            thinking_capability=thinking_capability,
            request=request,
        )
        changed = request.payload_generation != generation_before
        if legacy_request:
            request.serialize_provider_payload()
        return changed

    @staticmethod
    def _legacy_provider_request(context: ProxyRequestContext) -> ProviderBoundRequest:
        """Build a compatibility request for direct legacy helper callers.

        Production dispatch always supplies the request from the context. This
        narrow adapter keeps older unit-level helper callers working while
        preventing the pipeline itself from creating a competing request.

        The original body may carry a provider-namespace suffix on the
        ``model`` field (the form ``/v1/models`` exposes for provider-scoped
        entries). The upstream does not understand that suffix; the API
        handler normalizes the in-memory payload at request entry, and this
        helper mirrors that normalization so legacy embedders cannot
        accidentally forward a suffixed body.
        """
        body = context.original_body
        payload = jsonx_loads(body)
        if not isinstance(payload, dict):
            raise ValueError("provider request payload must be an object")
        typed_payload = cast("dict[str, Any]", payload)
        bound = ProviderBoundRequest(
            client_bytes=context.original_body,
            client_payload=typed_payload,
            client_protocol=context.protocol,
            model_id=context.model_id,
        )
        # Mirror the API handler's normalization: strip any ``/provider-id``
        # suffix from the in-memory model field. ``set_provider_payload``
        # detaches ``provider_payload`` from the aliased ``client_payload``
        # so the immutable contract is preserved.
        if context.model_id and typed_payload.get("model") != context.model_id:
            normalized = dict(typed_payload)
            normalized["model"] = context.model_id
            bound.set_provider_payload(normalized, increment_generation=False)
        return bound

    def _adapt_provider_thinking_controls(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        thinking_capability: ThinkingCapability,
        request: Any,
    ) -> None:
        """Validate and adapt thinking controls against the provider contract.

        Delegates to :func:`thinking_adaptation.adapt_provider_thinking_controls`.
        """
        from eggpool.request.thinking_adaptation import adapt_provider_thinking_controls

        adapt_provider_thinking_controls(
            context=context,
            selected=selected,
            thinking_capability=thinking_capability,
            request=request,
            catalog=self._catalog,
            config=self._config,
            transcoder_policy=self._transcoder_policy,
            resolve_provider_kind_fn=resolve_selected_provider_kind,
        )

    async def _finalize_selected_capability_rejection(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        err: CapabilityError,
    ) -> None:
        """Clean up selected-attempt state after a post-selection ``CapabilityError``.

        Provider-specific thinking-budget rejection (and any future
        post-selection client-validation failures) happens after the
        attempt row, reservation, active request count, and health slot
        have already been acquired in :meth:`_select_and_persist_attempt`.
        Without this helper, those side effects would remain visible until
        the retained terminal owner converged them.

        The cleanup runs inside a shielded finalizer call so ASGI
        task cancellation cannot strand the durable state in an
        intermediate form. The release reason ``capability_rejected``
        distinguishes this path from upstream-attempt failures
        (``attempt_failed`` / ``attempt_retryable`` /
        ``post_commit_interrupted``) so audit reports can attribute the
        outcome correctly.

        No upstream health penalty is applied — a capability rejection
        is a client-validation failure, not an account health signal.
        """
        elapsed_ms = self._elapsed_ms(context)
        rejection_reason = getattr(err, "reason", None) or "capability_rejected"
        if context.thinking_trace is not None:
            context.thinking_trace["decision"] = "rejected"
            context.thinking_trace["capability_status"] = rejection_reason
            context.thinking_trace["provider_id"] = selected.provider_id
        try:
            counter = get_counter()
            await counter.increment_rejected(
                client_protocol=context.protocol,
                capability_status=rejection_reason,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "thinking metrics counter unavailable for capability_rejected",
                exc_info=True,
            )
        try:
            await self._finalize_terminal(
                context,
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_ERROR,
                    status_code=400,
                    error_class=type(err).__name__,
                    error_detail=str(err),
                    upstream_latency_ms=elapsed_ms,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace,
                    ),
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
                ),
            )
        except AcceptedFinalizationInvariantError as finalize_err:
            raise finalize_err from err
        except DatabaseError as finalize_err:
            # Plan 142: fail closed. Do not silently report a clean 400
            # when the canonical finalization owner could not converge
            # the selected attempt state. The existing 500 fallback in
            # ``RequestCoordinator.execute`` and the request-level
            # exception handler own the response instead so the
            # supervisor/restart path can recover durable convergence.
            raise finalize_err from err

    async def _finalize_selected_transcode_loss_rejection(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        err: TranscodeLossError,
    ) -> None:
        """Clean up state after a post-selection ``TranscodeLossError``.

        Plan 142: when the configured cross-protocol ``loss_policy =
        "reject"`` (or a per-feature loss path) decides the selected
        provider cannot represent the client request, this helper
        converges the selected attempt durable/runtime ownership
        synchronously through the canonical finalization owner so the
        request does not strand in durable state. The terminal outcome
        is ``CLIENT_ERROR / 400`` because ``TranscodeLossError`` is a
        client-validation outcome, not an account health signal.

        No upstream health, backoff, or quarantine effect is applied.
        No thinking-specific trace/counter work runs here (mirrors the
        structure of :meth:`_finalize_selected_oversize_rejection` for
        the minimum code needed to render a 400).
        """
        elapsed_ms = self._elapsed_ms(context)
        try:
            await self._finalize_terminal(
                context,
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_ERROR,
                    status_code=400,
                    error_class=type(err).__name__,
                    error_detail=str(err),
                    upstream_latency_ms=elapsed_ms,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace,
                    ),
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
                ),
            )
        except AcceptedFinalizationInvariantError as finalize_err:
            raise finalize_err from err
        except DatabaseError as finalize_err:
            # Plan 142: fail closed. Propagate into the existing
            # supervisor/restart path; do not silently report a clean
            # 400 when durable convergence is unknown.
            raise finalize_err from err

    async def _finalize_selected_oversize_rejection(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        err: RequestTooLargeError,
    ) -> None:
        """Clean up state after a post-selection size rejection.

        Provider-bound serialized-size rejection is a local client-validation
        failure (HTTP 413): no upstream I/O occurred, no provider health
        penalty is applied, and no backoff or quarantine is recorded. The
        attempt row, reservation, active request count, and health slot
        acquired by :meth:`_select_and_persist_attempt` must be converged
        synchronously through the canonical finalization owner so the request
        does not strand in durable state.

        The ``_oversize_finalized`` flag is a **proof-of-convergence marker**:
        it is set only after the canonical finalization owner has established
        the required durable and runtime convergence. Earlier setting would
        let a later ``_handle_exhausted`` call skip convergence when the
        underlying durable finalization actually failed. On
        :class:`AcceptedFinalizationInvariantError` or :class:`DatabaseError`
        the finalization failure is propagated so the existing fail-closed
        recovery path can take ownership instead of silently reporting a clean
        413.
        """
        elapsed_ms = self._elapsed_ms(context)
        try:
            await self._finalize_terminal(
                context,
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_ERROR,
                    status_code=413,
                    error_class=type(err).__name__,
                    error_detail=str(err),
                    upstream_latency_ms=elapsed_ms,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace,
                    ),
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
                ),
            )
        except AcceptedFinalizationInvariantError as finalize_err:
            raise finalize_err from err
        except DatabaseError as finalize_err:
            # Do not silently swallow a durable finalization failure: the
            # marker is intentionally not set so the existing fail-closed
            # recovery path can take ownership of the request.
            raise finalize_err from err
        # Finalization has converged selected durable/runtime ownership;
        # mark the request so the retry loop's later ``_handle_exhausted``
        # call observes the existing terminal job and skips a conflicting
        # second finalization for the same attempt.
        context.client_metadata["_oversize_finalized"] = True

    async def _apply_selected_provider_transcode(
        self,
        *,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        transcoder: BodyTranscoder,
    ) -> None:
        """Perform definitive cross-protocol translation against selected provider.

        Plan 141: when the request carries provider-sensitive multimodal
        content (or the preflight prepared-transcode is otherwise not
        reusable), the definitive translation is performed here after
        ``SelectedAttempt`` exists. Capability metadata is resolved against
        ``selected.provider_id``; the translation always starts from the
        original client payload so a retry that selects a different
        provider does not stack the previous provider's translation on top
        of a different provider's contract.

        Text-only requests with a valid :class:`PreparedTranscode` already
        adopted the preflight translation in pre-selection and skip this
        helper. Native same-protocol requests do not reach it.
        """
        provider_bound = context.provider_bound
        if provider_bound is None:
            return
        _features = (
            self._transcoder_policy.features
            if self._transcoder_policy is not None
            else None
        )
        _thinking_off = _features is None or not getattr(_features, "thinking", False)
        _client_has_thinking = self._client_has_thinking_controls(
            context.original_body,
            context.protocol,
            parsed_payload=context.parsed_payload,
        )
        _has_provider_sensitive_media = (
            self._client_payload_has_provider_sensitive_media(context)
        )
        # If a valid preflight prepared-transcode was already adopted
        # during pre-selection (text-only path), this helper is a no-op.
        if not _has_provider_sensitive_media and self._prepared_eligible(
            prepared_transcode=context.prepared_transcode,
            features=_features,
            upstream_protocol=(
                context.transcode_context.upstream_protocol
                if context.transcode_context is not None
                else None
            ),
            thinking_off=_thinking_off,
            client_has_thinking=_client_has_thinking,
        ):
            return
        # Always translate from the original client payload so retries
        # against a different selected provider start clean. The adopted
        # translated graph from a previous attempt is discarded; the
        # ``client_payload`` remains immutable per ProviderBoundRequest's
        # ownership contract.
        #
        # ``client_payload`` may still carry a provider-namespace suffix
        # (``model-id/provider-id``) -- ``/v1/models`` exposes provider-
        # scoped entries in that form. The transcoders copy the ``model``
        # field verbatim, so passing the suffixed client payload straight
        # to the encoder would forward the suffix to upstream, which
        # rejects it. Strip the suffix here so any re-translation path
        # -- cross-protocol transcoding with provider-sensitive media or
        # thinking controls, or a retry that selects a different provider
        # -- produces a body whose ``model`` field is the bare upstream id.
        reset_payload: Mapping[str, Any] = provider_bound.client_payload
        if (
            provider_bound.model_id
            and isinstance(reset_payload, dict)
            and reset_payload.get("model") != provider_bound.model_id
        ):
            normalized = dict(reset_payload)
            normalized["model"] = provider_bound.model_id
            reset_payload = normalized
        provider_bound.set_provider_payload(
            reset_payload,
            increment_generation=True,
        )
        _thinking_cap: ThinkingCapability | None = None
        _transcoding_cap = None
        _multimodal_cap = None
        _budget_defaults: dict[str, int] | None = None
        _budget_policy = "lenient"
        _loss_policy = "warn"
        if self._transcoder_policy is not None:
            _budget_cfg = self._transcoder_policy.thinking_budget_defaults
            _budget_defaults = _budget_cfg.as_dict()
            _budget_policy = self._transcoder_policy.budget_resolution_policy
            _loss_policy = self._transcoder_policy.loss_policy
        # Plan 141: resolve capability metadata from the catalog cache
        # against ``selected.provider_id`` only. Collapsed models may be
        # served by multiple providers with different multimodal/request-
        # size contracts; the global first-seen entry is not authoritative
        # once a provider has been selected.
        try:
            from eggpool.catalog.capabilities import dict_to_model_capabilities

            model_info = self._catalog.cache.get_model_for_provider(
                context.model_id,
                selected.provider_id,
            )
            if model_info is not None:
                caps_raw: dict[str, Any] = model_info.get("capabilities", {})  # type: ignore[assignment]
                caps = dict_to_model_capabilities(caps_raw)
                _thinking_cap = caps.thinking
                _transcoding_cap = caps.transcoding
                _multimodal_cap = caps.multimodal
        except Exception:  # noqa: BLE001
            pass  # best-effort; resolver has its own fallbacks
        payload = provider_bound.provider_payload
        if not isinstance(payload, dict):
            return
        typed_payload = cast("dict[str, Any]", payload)
        transcode_ctx = context.transcode_context
        if transcode_ctx is None:
            return
        translated, warnings = transcoder.encode_request(
            typed_payload,
            transcode_ctx,
            features=_features,
            thinking_capability=_thinking_cap,
            transcoding_capability=_transcoding_cap,
            multimodal_capability=_multimodal_cap,
            budget_defaults=_budget_defaults,
            budget_resolution_policy=_budget_policy,
            loss_policy=_loss_policy,
        )
        # The encoder owns the fresh target graph. Adopt it directly so
        # this changed protocol generation does not incur a second equality
        # walk or recursive ownership pass.
        provider_bound.adopt_provider_payload(
            translated,
            reason="protocol_transcode",
        )
        transcode_ctx.loss_warnings.extend(warnings)

        # Determine thinking decision from transcoder warnings using the
        # canonical kind-based classifier (Phase D).
        if context.thinking_trace is not None:
            from eggpool.catalog.capabilities import (
                classify_thinking_warning_decision,
                is_thinking_warning,
            )

            all_warnings = transcode_ctx.loss_warnings
            decision = classify_thinking_warning_decision(all_warnings)
            context.thinking_trace["decision"] = decision
            context.thinking_trace["provider_id"] = selected.provider_id
            thinking_warnings = [w for w in all_warnings if is_thinking_warning(w)]
            if decision == "clamped" and any(
                w.get("kind") == "budget_clamped" for w in thinking_warnings
            ):
                context.thinking_trace["budget_clamped"] = True
            # Surface resolved budget + upstream field metadata whenever
            # the early translation has produced a concrete ``thinking``
            # block. Phase C supplements this in the dispatch path with the
            # selected provider's override.
            thinking_block_obj: object = translated.get("thinking")  # pyright: ignore[reportUnknownMemberType]
            if isinstance(thinking_block_obj, dict):
                thinking_block: dict[str, object] = thinking_block_obj  # pyright: ignore[reportUnknownVariableType]
                budget_value_obj: object = thinking_block.get("budget_tokens")  # pyright: ignore[reportUnknownMemberType]
                if isinstance(budget_value_obj, int) and not isinstance(
                    budget_value_obj, bool
                ):
                    budget_value = budget_value_obj
                    context.thinking_trace["resolved_budget_tokens"] = budget_value
                    if not context.thinking_trace.get("upstream_fields"):
                        context.thinking_trace["upstream_fields"] = ["thinking"]
            if context.upstream_protocol == "anthropic":
                context.thinking_trace["upstream_protocol"] = context.upstream_protocol

            _thinking_counter = get_counter()
            client_proto = context.thinking_trace["client_protocol"]
            if decision == "transcoded":
                await _thinking_counter.increment_transcoded(
                    client_protocol=client_proto,
                    upstream_protocol=context.upstream_protocol or "unknown",
                    provider_id=selected.provider_id,
                )
            elif decision == "dropped":
                await _thinking_counter.increment_dropped(
                    client_protocol=client_proto,
                    upstream_protocol=context.upstream_protocol or "unknown",
                    reason="reasoning_content_dropped",
                )
            elif decision == "clamped":
                await _thinking_counter.increment_budget_clamped(
                    client_protocol=client_proto,
                    provider_id=selected.provider_id,
                )
            elif decision == "rejected":
                await _thinking_counter.increment_rejected(
                    client_protocol=client_proto,
                    capability_status="budget_rejected",
                )

    @staticmethod
    def _prepared_eligible(
        *,
        prepared_transcode: Any,
        features: Any,
        upstream_protocol: str | None,
        thinking_off: bool,
        client_has_thinking: bool,
    ) -> bool:
        """Return True when a preflight PreparedTranscode is still reusable.

        Mirrors the pre-selection validity check so the post-selection
        helper can skip translation when the prepared-transcode fast path
        already produced a valid translated payload.
        """
        if prepared_transcode is None or upstream_protocol is None:
            return False
        if not prepared_transcode.is_valid_for(
            upstream_protocol=upstream_protocol,
            features=features,
        ):
            return False
        return thinking_off or not client_has_thinking

    def _build_upstream_headers(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
    ) -> dict[str, str]:
        """Build upstream headers using provider contract when available."""
        from eggpool.proxy.client import sanitize_request_headers

        sanitized = sanitize_request_headers(context.incoming_headers)
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
        provider_id: str | None = None,
        upstream_protocol: str = "openai",
        client_protocol: str = "openai",
        effect_progress: FailureEffectProgress | None = None,
    ) -> None:
        """Apply health transitions for a failed account.

        Plan 025: route the failure through the typed effects
        classifier and apply the resulting
        :class:`FailureEffects` exactly once via
        :class:`EffectsApplier`.  Backwards compatibility: the legacy
        :func:`classify_failure_category` path is preserved as the
        no-applier fallback so test doubles that inject a ``None``
        applier still produce the same final state machine
        transitions.
        """
        if self._health_manager is None:
            return

        if self._effects_applier is not None:
            await self._apply_failure_effects(
                account_name=account_name,
                model_id=model_id,
                provider_id=provider_id,
                upstream_protocol=upstream_protocol,
                client_protocol=client_protocol,
                err=err,
                effect_progress=effect_progress,
            )
            return

        category = classify_failure_category(err.error_class, err.status_code)
        rate_limit_retry_after: float | None = None
        backoff_until_epoch: float | None = None
        if category == FailureCategory.AUTHENTICATION_FAILED:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason="authentication_failed"
            )
            # Terminal; the repository persists this as a NULL deadline.
            backoff_until_epoch = None
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
            delay = compute_backoff_seconds(
                category.value,
                consecutive_failures=self._health_manager.get_account_health(
                    account_name
                ).consecutive_failures,
                jitter=False,
            )
            if delay is None:
                delay = 300.0
            self._health_manager.disable_model(
                account_name,
                model_id,
                duration_seconds=delay,
            )
            self._health_manager.release_request(account_name)
            self._catalog.cache.mark_model_unavailable(account_name, model_id)
            backoff_until_epoch = time.time() + delay
        else:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason=category.value
            )
            # Transient reasons get a short exponential cooldown so a
            # restart does not silently clear them.
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

    async def _apply_failure_effects(
        self,
        *,
        account_name: str,
        model_id: str,
        provider_id: str | None,
        upstream_protocol: str,
        client_protocol: str,
        err: _RetryableUpstreamError,
        effect_progress: FailureEffectProgress | None = None,
    ) -> None:
        """Apply Plan 025 typed failure effects via :class:`EffectsApplier`.

        Builds a :class:`FailureObservation` from the upstream error
        and routes it through the pure effects classifier.  Effects
        are applied once per attempt identity and backoff is
        persisted using the existing :func:`_persist_backoff` helper
        so the SQLite contract is unchanged.
        """
        if self._effects_applier is None:
            return

        observation = err.failure_observation or FailureObservation(
            source=err.source,
            status_code=err.status_code,
            error_class=err.error_class,
            provider_id=provider_id,
            account_name=account_name,
            model_id=model_id,
            upstream_model_id=model_id,
            client_protocol=client_protocol,
            upstream_protocol=upstream_protocol,
            response_signal=None,
            retry_after_s=err.retry_after,
            response_started=False,
        )
        effects = err.failure_effects or classify_failure_effects(observation)

        attempt_key = (
            f"{observation.proxy_request_id or account_name}:"
            f"{observation.attempt_id or err.status_code or 'unselected'}"
        )

        # The applier mutates health manager / quarantine / circuit
        # breaker exactly once.  We only need to persist backoff and
        # update runtime state below.
        self._effects_applier.apply_once(
            attempt_key=attempt_key,
            observation=observation,
            effects=effects,
            progress=effect_progress,
        )

        # Persist backoff using the same helper as the legacy path.
        if effects.persist_backoff and effects.backoff_reason:
            if (
                effect_progress is None
                or not effect_progress.backoff_persistence_completed
            ):
                if effect_progress is not None:
                    effect_progress.backoff_persistence_attempted = True
                await self._persist_backoff(
                    account_name=account_name,
                    model_id=model_id
                    if effects.model_effect in ("quarantine", "terminal_withdrawal")
                    else None,
                    reason=effects.backoff_reason,
                    status_code=err.status_code,
                    error_class=err.error_class,
                    backoff_until=effects.backoff_until,
                    consecutive_failures=(
                        self._health_manager.get_account_health(
                            account_name
                        ).consecutive_failures
                        if self._health_manager is not None
                        else 0
                    ),
                )
                if effect_progress is not None:
                    effect_progress.backoff_persistence_completed = True
        elif effect_progress is not None:
            effect_progress.backoff_persistence_attempted = True
            effect_progress.backoff_persistence_completed = True

        # Update runtime state with the normalized category so the
        # routing layer can still observe failure counts even when the
        # effects-applier handles health transitions.
        state = self._registry.get_state(account_name)
        if state is not None and effects.account_effect != "none":
            state.record_failure(
                effects.backoff_reason or effects.evidence_class,
                cooldown_seconds=self._quota_exhausted_cooldown_seconds,
                rate_limit_retry_after=err.retry_after
                if effects.account_effect == "rate_limit"
                else None,
            )

        # Clear the bound probe slot for request-local paths when the
        # applier didn't already (model_effect != none already releases
        # via _apply_model_effect; release_probe_only paths release via
        # _apply_probe_release).  This branch is a no-op when the
        # applier already released the slot, because
        # ``release_request`` is idempotent on the half-open flag.
        if (
            effects.release_probe_only
            and effects.account_effect == "none"
            and self._health_manager is not None
        ):
            self._health_manager.release_request(account_name)

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

        Delegates to :func:`backoff_persistence.persist_backoff`.
        """
        from eggpool.request.backoff_persistence import persist_backoff

        await persist_backoff(
            account_backoff_repo=self._account_backoff_repo,
            account_id_cache=self._account_id_cache,
            db=self._db,
            account_name=account_name,
            model_id=model_id,
            reason=reason,
            status_code=status_code,
            error_class=error_class,
            backoff_until=backoff_until,
            consecutive_failures=consecutive_failures,
        )

    async def _clear_backoff(
        self,
        account_name: str,
        *,
        model_id: str | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        """Remove persisted backoff rows for a successful request.

        Delegates to :func:`backoff_persistence.clear_backoff`.
        """
        from eggpool.request.backoff_persistence import clear_backoff

        await clear_backoff(
            account_backoff_repo=self._account_backoff_repo,
            account_id_cache=self._account_id_cache,
            db=self._db,
            account_name=account_name,
            model_id=model_id,
            reasons=reasons,
        )

    async def _finalize_non_retryable(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        status_code: int,
        resp_headers: list[tuple[str, str]],
        resp_body: bytes,
        *,
        failure_observation: FailureObservation | None = None,
        failure_effects: FailureEffects | None = None,
    ) -> None:
        """Finalize a non-retryable client error (4xx)."""
        elapsed_ms = self._elapsed_ms(context)
        upstream_connect_ms = context.upstream_connect_ms
        upstream_read_ms = self._upstream_read_ms(context, elapsed_ms)
        await self._finalize_terminal(
            context,
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.CLIENT_ERROR,
                status_code=status_code,
                upstream_latency_ms=elapsed_ms,
                bytes_emitted=len(resp_body),
                upstream_request_id=self._get_header_value(
                    resp_headers, _UPSTREAM_REQUEST_ID_HEADERS
                ),
                bytes_received=context.original_body_size or len(context.original_body),
                upstream_protocol=context.upstream_protocol,
                first_byte_ms=self._upstream_header_ms(context),
                upstream_connect_ms=upstream_connect_ms,
                upstream_read_ms=upstream_read_ms,
                coordinator_overhead_ms=self._coordinator_overhead_ms(
                    total_ms=elapsed_ms,
                    connect_ms=upstream_connect_ms,
                    read_ms=upstream_read_ms,
                ),
                failure_observation=failure_observation,
                failure_effects=failure_effects,
                thinking_trace_json=_serialize_thinking_trace(context.thinking_trace),
                segmentation=context.segmentation,
                segmentation_not_collected=context.segmentation_not_collected,
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
        if last_selected is not None and not context.client_metadata.get(
            "_oversize_finalized"
        ):
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
                    (
                        isinstance(last_error, _RetryableUpstreamError)
                        and last_error.error_class is not None
                    )
                    or (
                        isinstance(last_error, _NonRetryableUpstreamError)
                        and last_error.error_class is not None
                    )
                    or isinstance(last_error, _LocalDispatchError)
                ):
                    error_class = last_error.error_class
                else:
                    error_class = type(last_error).__name__
                error_detail = _prepare_error_detail(
                    last_error, self._persist_error_detail
                )
                if isinstance(last_error, _NonRetryableUpstreamError):
                    outcome = FinalizationOutcome.CLIENT_ERROR
                    health_already_applied = health_applied
                elif isinstance(last_error, _RetryableUpstreamError):
                    outcome = FinalizationOutcome.UPSTREAM_ERROR
                    health_already_applied = health_applied
                elif isinstance(last_error, _LocalDispatchError):
                    # Local preparation/transcoding failures have zero
                    # provider effects (see ``failure/classifier.py``):
                    # persist them as client errors so account runtime
                    # state and per-account error stats are not skewed
                    # as upstream failures.
                    outcome = FinalizationOutcome.CLIENT_ERROR

            upstream_connect_ms = None
            upstream_read_ms = None
            coordinator_overhead_ms = None
            first_byte_ms = None
            bytes_emitted = 0
            upstream_request_id = None
            if isinstance(
                last_error, (_NonRetryableUpstreamError, _LocalDispatchError)
            ):
                first_byte_ms = self._upstream_header_ms(context)
                upstream_connect_ms = context.upstream_connect_ms
                upstream_read_ms = self._upstream_read_ms(context, elapsed_ms)
                coordinator_overhead_ms = self._coordinator_overhead_ms(
                    total_ms=elapsed_ms,
                    connect_ms=upstream_connect_ms,
                    read_ms=upstream_read_ms,
                )
                if last_upstream_response is not None:
                    _, response_headers, response_body = last_upstream_response
                    upstream_request_id = self._get_header_value(
                        response_headers, _UPSTREAM_REQUEST_ID_HEADERS
                    )
                    bytes_emitted = len(response_body)

            await self._finalize_terminal(
                context,
                last_selected,
                FinalizationData(
                    outcome=outcome,
                    status_code=status_code,
                    error_class=error_class,
                    error_detail=error_detail,
                    upstream_latency_ms=elapsed_ms,
                    first_byte_ms=first_byte_ms,
                    bytes_emitted=bytes_emitted,
                    downstream_started=context.response_handoff.started,
                    health_already_applied=health_already_applied,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    upstream_request_id=upstream_request_id,
                    upstream_connect_ms=upstream_connect_ms,
                    upstream_read_ms=upstream_read_ms,
                    coordinator_overhead_ms=coordinator_overhead_ms,
                    failure_observation=(
                        getattr(last_error, "failure_observation", None)
                        if last_error is not None
                        else None
                    ),
                    failure_effects=(
                        getattr(last_error, "failure_effects", None)
                        if last_error is not None
                        else None
                    ),
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
                ),
            )
            # Record first-class HTTPX transport outcome for streaming
            # requests that exhaust retries or hit non-retryable errors.
            if (
                context.streaming
                and error_class is not None
                and isinstance(last_error, _RetryableUpstreamError)
            ):
                first_class_outcome = classify_httpx_error_class(error_class)
                self._stream_diagnostics.record_outcome(
                    first_class_outcome,
                    proxy_request_id=context.request_id,
                    db_request_id=last_selected.db_request_id,
                    provider_id=last_selected.provider_id,
                    account_name=last_selected.account_name,
                    model_id=last_selected.model_id,
                    protocol=context.upstream_protocol,
                    elapsed_ms=elapsed_ms,
                    attempt=last_selected.attempt_number,
                    exception_class=error_class,
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
            await self._finalize_terminal(
                context,
                synthetic,
                FinalizationData(
                    outcome=FinalizationOutcome.UPSTREAM_ERROR,
                    status_code=status_code,
                    error_class=error_class,
                    error_detail=error_detail,
                    upstream_latency_ms=elapsed_ms,
                    downstream_started=context.response_handoff.started,
                    bytes_received=context.original_body_size
                    or len(context.original_body),
                    upstream_protocol=context.upstream_protocol,
                    thinking_trace_json=_serialize_thinking_trace(
                        context.thinking_trace
                    ),
                    segmentation=context.segmentation,
                    segmentation_not_collected=context.segmentation_not_collected,
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
            if context.client_metadata.get("attempt_ceiling_reached"):
                resp_headers.append(("x-proxy-retry-reason", "attempt_ceiling_reached"))
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
                        err_payload_obj: object = jsonx_loads(body)
                    except ValueError:
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
                    try:
                        _status, err_body, err_warnings = transcoder.reencode_error(
                            status, err_payload, transcode_ctx
                        )
                        body = encode_json_body(err_body)
                        transcode_ctx.loss_warnings.extend(err_warnings)
                    except Exception:
                        logger.warning(
                            "Exhausted error response adaptation failed; preserving "
                            "filtered upstream body: request_id=%s",
                            context.request_id,
                            exc_info=True,
                        )
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
            error_body = encode_json_body(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": error_msg,
                    },
                }
            )
        else:
            error_body = encode_json_body(
                {
                    "error": {
                        "message": error_msg,
                        "type": "server_error",
                        "code": status_code,
                    }
                }
            )
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

    def _client_has_thinking_controls(
        self,
        original_body: bytes,
        protocol: str,
        *,
        parsed_payload: ParsedRequestPayload | None = None,
    ) -> bool:
        """Return True when the client request contains thinking/reasoning controls.

        Delegates to :func:`thinking_adaptation.client_has_thinking_controls`.
        """
        from eggpool.request.thinking_adaptation import client_has_thinking_controls

        return client_has_thinking_controls(
            original_body, protocol, parsed_payload=parsed_payload
        )

    def _client_payload_has_provider_sensitive_media(
        self,
        context: ProxyRequestContext,
    ) -> bool:
        """Return True when the client payload needs provider-scoped recompute.

        Capabilities such as image source forms, document support, and
        tool-result media are resolved against the *selected* provider.
        The preflight translation was completed before provider
        selection, so a cached :class:`PreparedTranscode` cannot be
        safely reused when the request contains any of those forms.
        Forces a final recompute against the selected provider's row.
        """
        from eggpool.transcoder.sensitive_media import (
            request_has_provider_sensitive_media,
        )

        if context.parsed_payload is not None:
            payload = context.parsed_payload.parsed_dict
            if isinstance(payload, dict):
                return request_has_provider_sensitive_media(payload)
        return False

    def _all_accounts_attempted(
        self,
        context: ProxyRequestContext,
        *,
        capability_policy: dict[str, str] | None = None,
    ) -> bool:
        """Return whether every eligible account has been attempted.

        Used by the retry loop to distinguish pre-dispatch
        unavailability (genuine 503) from post-retry exhaustion
        (502 ``UpstreamExhaustedError``). ``True`` when the
        eligible account set is non-empty and every name is
        already in ``context.attempted_accounts``.
        """
        eligible = self._router.get_eligible_account_names(
            context.model_id,
            provider_id=context.provider_id,
            protocol=context.upstream_protocol,
            transcode_eligibility=(
                {context.protocol, context.upstream_protocol}
                if context.transcode_required
                else None
            ),
            thinking_requirement=(
                context.thinking_requirement
                if context.thinking_requirement is not None
                and context.thinking_requirement.required
                else None
            ),
            capability_policy=capability_policy,
            request_surface=getattr(context, "request_surface", "chat_completions"),
        )
        if not eligible:
            return False
        attempted = context.attempted_accounts
        return all(account_name in attempted for account_name in eligible)

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

        Delegates to :func:`routing_helpers.build_score_components`.
        """
        from eggpool.request.routing_helpers import build_score_components

        return build_score_components(
            ranked_candidates=ranked_candidates,
            selected_account_name=selected_account_name,
            selected_state=selected_state,
            selected_score=selected_score,
            selected_tier=selected_tier,
            fairness_decision=fairness_decision,
            fairness_band_names=fairness_band_names,
        )

    @staticmethod
    def _build_top_candidates(
        ranked_candidates: list[tuple[Any, Any]],
        *,
        limit: int = 5,
        fairness_band_names: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Render the top-N ranked candidates for the dashboard table.

        Delegates to :func:`routing_helpers.build_top_candidates`.
        """
        from eggpool.request.routing_helpers import build_top_candidates

        return build_top_candidates(
            ranked_candidates,
            limit=limit,
            fairness_band_names=fairness_band_names,
        )

    @staticmethod
    def _derive_tie_break_summary(
        *,
        ranked_candidates: list[tuple[Any, Any]],
        selected_score_obj: Any | None,
    ) -> dict[str, Any]:
        """Summarise why the selected account won over its runner-up.

        Delegates to :func:`routing_helpers.derive_tie_break_summary`.
        """
        from eggpool.request.routing_helpers import derive_tie_break_summary

        return derive_tie_break_summary(
            ranked_candidates=ranked_candidates,
            selected_score_obj=selected_score_obj,
        )

    def _resolve_upstream_protocol(
        self,
        context: ProxyRequestContext,
    ) -> str | None:
        """Determine the upstream protocol for transcoding.

        Delegates to :func:`upstream_helpers.resolve_upstream_protocol`.
        """
        from eggpool.request.upstream_helpers import resolve_upstream_protocol

        return resolve_upstream_protocol(
            context,
            catalog=self._catalog,
            transcoder_policy=self._transcoder_policy,
        )

    def _validate_endpoint_or_transcode(self, context: ProxyRequestContext) -> None:
        """Validate that the endpoint matches the model's protocol.

        Delegates to :func:`upstream_helpers.validate_endpoint_or_transcode`.
        """
        from eggpool.request.upstream_helpers import validate_endpoint_or_transcode

        validate_endpoint_or_transcode(
            context,
            catalog=self._catalog,
            transcoder_policy=self._transcoder_policy,
        )

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
        """Map an exception to an HTTP status code.

        Delegates to :func:`static_helpers.error_status_code`.
        """
        from eggpool.request.static_helpers import error_status_code

        return error_status_code(err)


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
        failure_observation: FailureObservation | None = None,
        failure_effects: FailureEffects | None = None,
        source: str = "transport",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_class = error_class
        self.retry_after = retry_after
        self.upstream_response = upstream_response
        self.retry_category = retry_category
        self.failure_observation = failure_observation
        self.failure_effects = failure_effects
        self.source = source


class _LocalDispatchError(Exception):
    """A bounded local failure that must never be retried as provider fault."""

    def __init__(
        self,
        *,
        stage: str,
        error_class: str,
        failure_observation: FailureObservation,
        failure_effects: FailureEffects,
    ) -> None:
        super().__init__(f"local dispatch failure in {stage}")
        self.stage = stage
        self.error_class = error_class
        self.failure_observation = failure_observation
        self.failure_effects = failure_effects


class _LocalStreamTranslationError(Exception):
    """A response-frame adaptation failure owned by EggPool."""


class _NonRetryableUpstreamError(Exception):
    """An upstream error that should not be retried."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        error_class: str | None = None,
        upstream_response: tuple[int, list[tuple[str, str]], bytes] | None = None,
        failure_observation: FailureObservation | None = None,
        failure_effects: FailureEffects | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_class = error_class
        self.upstream_response = upstream_response
        self.failure_observation = failure_observation
        self.failure_effects = failure_effects
