"""Shared request handling for protocol-compatible proxy endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import typing
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException

from eggpool.api.errors import (
    anthropic_capability_error_response,
    openai_capability_error_response,
)
from eggpool.auth import require_auth
from eggpool.catalog.capabilities import classify_thinking_request
from eggpool.catalog.protocols import ProtocolMismatchError, ProtocolName
from eggpool.errors import (
    AccountSuspendedError,
    CapabilityError,
    CatalogUnavailableError,
    ContextLimitExceededError,
    ModelNotFoundError,
    ModelUnavailableError,
    NoEligibleAccountError,
    RequestTooLargeError,
    UpstreamExhaustedError,
)
from eggpool.jsonx import loads as jsonx_loads
from eggpool.model_router.affinity import (
    AFFINITY_SESSION_HEADER,
    ModelRouterAffinity,
    automatic_session_identity,
    session_identity_from_header,
)
from eggpool.model_router.registry import ModelRouterRegistry
from eggpool.model_router.selector import ModelRouterSelector
from eggpool.request.body import encode_json_body, read_body_limited
from eggpool.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.request.limits import (
    check_context_limits as _check_context_limits,
)
from eggpool.request.limits import (
    estimate_json_value_tokens,
    estimate_reservation_tokens,
)
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.routing.provider import parse_model_provider
from eggpool.runtime_dispatch import (
    SPAN_AUTH,
    SPAN_BODY_READ,
    SPAN_CONTEXT_BUILD,
    SPAN_CONTEXT_LIMIT,
    SPAN_JSON_PARSE,
    SPAN_MODEL_PARSE,
    SPAN_TRANSCODE_PREFLIGHT,
    DispatchSpanRecorder,
)
from eggpool.runtime_manager import GenerationLease, wrap_stream_with_lease
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.prepared import PreparedTranscode
from eggpool.wire.ir import (
    CanonicalRequest,
    CanonicalSurface,
    canonical_request_from_mapping,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from fastapi import Request
    from starlette.types import Message, Receive, Scope, Send

    from eggpool.request.response_handoff import ResponseHandoffState

logger = logging.getLogger(__name__)


class ProxyStreamingResponse(StreamingResponse):
    """Streaming response that records the ASGI response-start boundary."""

    def __init__(
        self,
        content: Any,
        *,
        response_handoff: ResponseHandoffState,
        status_code: int = 200,
        headers: Any = None,  # noqa: ANN401
        media_type: str | None = None,
        background: Any = None,  # noqa: ANN401
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self.response_handoff = response_handoff

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def handoff_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.response_handoff.mark_started()
            await send(message)

        await super().__call__(scope, receive, handoff_send)


class ErrorResponseFactory(Protocol):
    """Callable contract implemented by protocol-specific error renderers."""

    def __call__(
        self,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
    ) -> JSONResponse: ...


def _validate_responses_stateless(payload: dict[str, Any]) -> str | None:
    """Return a rejection message when a Responses payload is not stateless.

    EggPool exposes ``POST /v1/responses`` as a stateless client surface.
    Stateful Responses features (``previous_response_id``,
    conversation references, ``store = true``, ``background = true``) bind
    a request to a specific upstream's response identity and cannot be
    safely failed over across accounts. This helper detects those fields
    and returns a concise rejection message; ``None`` means the payload
    is stateless and may continue.

    The check happens before durable account selection so the operator's
    client never receives a partial success followed by a retry on a
    different provider.

    Plan 144 (D): the stateless contract is enforced precisely:
    - any non-None ``previous_response_id`` is rejected;
    - any non-None ``conversation`` is rejected (including ``{}``);
    - ``store`` must be explicitly ``false``;
    - ``background = true`` is rejected.
    """
    previous = payload.get("previous_response_id")
    if previous is not None:
        return (
            "EggPool's /v1/responses is stateless only; "
            "previous_response_id is not supported."
        )
    conversation = payload.get("conversation")
    if conversation is not None:
        return (
            "EggPool's /v1/responses is stateless only; "
            "conversation references are not supported."
        )
    store = payload.get("store")
    if store is False:
        pass  # explicit false allowed
    elif store is True:
        return "EggPool's /v1/responses is stateless only; store=true is not supported."
    elif store is None:
        return (
            "EggPool's /v1/responses is stateless only; "
            "store=false must be explicitly set."
        )
    else:
        return (
            "EggPool's /v1/responses is stateless only; store must be explicitly false."
        )
    if payload.get("background") is True:
        return (
            "EggPool's /v1/responses is stateless only; "
            "background=true is not supported."
        )
    return None


@dataclass(frozen=True)
class ProxyEndpointConfig:
    """Protocol-specific behavior for the shared proxy endpoint pipeline.

    Attributes
    ----------
    protocol:
        The translation family of the client endpoint. Responses and
        Chat Completions both share ``"openai"``; the wire surface is
        selected via ``request_surface``.
    request_surface:
        Identifies the wire endpoint surface served by this handler.
        Defaults to ``"chat_completions"`` so existing call sites keep
        their current dispatch behavior. ``"responses"`` selects the
        stateless OpenAI Responses surface; the field is a *surface*
        declaration, not a new
        ``ProtocolName``.
    request_label:
        Human-readable label used for logging.
    error_response:
        Callable that renders a protocol-shaped error response.
    not_found_error_type / service_error_type:
        Protocol-specific error type strings for 404 / 5xx responses.
    """

    protocol: ProtocolName
    request_label: str
    error_response: ErrorResponseFactory
    not_found_error_type: str
    service_error_type: str
    request_surface: str = "chat_completions"


@dataclass(frozen=True)
class TranscodePreflightResult:
    """Result of translating a request body before durable dispatch."""

    upstream_protocol: ProtocolName
    translated_payload: dict[str, Any]
    warnings: list[dict[str, Any]]
    tool_token_padding: int = 0
    canonical_request: CanonicalRequest | None = None


def _canonical_surface_for_endpoint(endpoint: ProxyEndpointConfig) -> CanonicalSurface:
    """Map public endpoint naming to the canonical client-surface name."""
    if endpoint.protocol == "anthropic":
        return "messages"
    if endpoint.request_surface == "responses":
        return "responses"
    return "chat_completions"


def _tool_token_padding(payload: dict[str, Any]) -> int:
    """Estimate extra input tokens from tool schemas in a translated payload.

    Anthropic tool schemas (``input_schema``) are typically 30 % of their
    JSON size in tokens.  Reuse the decoded structural estimator instead of
    encoding each nested tool independently.  The minimum keeps this rough
    guardrail conservative for small schemas.
    """
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0
    return max(64, estimate_json_value_tokens(cast("list[object]", tools)))


_MAX_FORWARDED_CLIENT_IP_CHARS = 64


def _valid_forwarded_client_ip(value: str | None) -> str | None:
    """Return one bounded attribution value, or ignore malformed input."""
    if value is None:
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _MAX_FORWARDED_CLIENT_IP_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        return None
    return candidate


def get_client_ip(
    request: Request,
    *,
    trusted_proxies: Collection[str] = (),
) -> str:
    """Return the peer address, honoring forwarding only from trusted peers."""
    peer = request.client.host if request.client and request.client.host else ""
    if peer in trusted_proxies:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for is not None:
            # Walk from the EggPool-facing side of the chain.  The first
            # valid hop that is not itself trusted is the nearest identity
            # that the configured proxy chain can attest to; leftmost values
            # remain client-controlled when a proxy appends rather than
            # overwrites the header.
            for part in reversed(forwarded_for.split(",")):
                forwarded_ip = _valid_forwarded_client_ip(part)
                if forwarded_ip is not None and forwarded_ip not in trusted_proxies:
                    return forwarded_ip

        real_ip = _valid_forwarded_client_ip(request.headers.get("x-real-ip"))
        if real_ip is not None:
            return real_ip

    return peer


def render_proxy_response(result: PreparedProxyResponse) -> Response:
    """Render a prepared response without re-encoding its body or headers."""
    if result.stream_iterator is not None:
        response: Response = ProxyStreamingResponse(
            result.stream_iterator,
            status_code=result.status_code,
            media_type=None,
            response_handoff=result.response_handoff,
        )
    else:
        response = Response(
            content=result.body,
            status_code=result.status_code,
            media_type=None,
        )

    from eggpool.proxy.client import HOP_BY_HOP_HEADERS

    connection_tokens = {
        token.strip().casefold()
        for name, value in result.headers
        if name.casefold() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    for name, value in result.headers:
        lower_name = name.casefold()
        if (
            lower_name in HOP_BY_HOP_HEADERS
            or lower_name in connection_tokens
            or lower_name in {"content-encoding", "content-length"}
        ):
            continue
        response.headers.append(name, value)
    return response


def _infer_upstream_protocol(
    catalog: Any,  # noqa: ANN401
    model_id: str,
    client_protocol: str,
    provider_id: str | None = None,
) -> str | None:
    """Infer the upstream protocol for transcoding, or None on miss."""
    model_protocols = catalog.cache.get_model_protocols(
        model_id,
        provider_id=provider_id,
    )
    if client_protocol in model_protocols:
        return client_protocol

    candidates = catalog.cache.get_transcodable_protocols(
        model_id,
        client_protocol=client_protocol,
        provider_id=provider_id,
    )
    if not candidates:
        return None

    counts = {
        p: catalog.cache.count_eligible_accounts_for_protocol(
            model_id,
            p,
            provider_id=provider_id,
        )
        for p in candidates
    }
    return max(sorted(counts), key=lambda p: counts[p]) if counts else None


def _prepare_transcode_preflight(
    *,
    catalog: Any,  # noqa: ANN401
    model_id: str,
    provider_id: str | None,
    client_protocol: ProtocolName,
    payload: dict[str, Any],
    transcoder_policy: Any,  # noqa: ANN401
    client_surface: CanonicalSurface = "chat_completions",
    canonical_request: CanonicalRequest | None = None,
) -> TranscodePreflightResult | None:
    """Translate once for preflight checks when transcoding is active.

    Translation is on by default. The ``enabled`` flag on
    ``transcoder_policy`` is a deprecated escape hatch — when it is
    explicitly ``False`` translation is skipped (preserving the legacy
    protocol-exact behaviour). ``None`` and ``True`` both allow the
    preflight to run, so a missing policy object never silently disables
    translation.
    """
    if transcoder_policy is not None and transcoder_policy.enabled is False:
        return None

    upstream_protocol = _infer_upstream_protocol(
        catalog,
        model_id,
        client_protocol,
        provider_id,
    )
    if upstream_protocol is None or upstream_protocol == client_protocol:
        return None

    from eggpool.transcoder.protocol import select_transcoder

    transcoder = select_transcoder(
        client_protocol=client_protocol,
        upstream_protocol=upstream_protocol,
    )
    if transcoder is None:
        return None

    transcode_context = TranscodeContext(
        request_id="preflight",
        client_protocol=client_protocol,
        upstream_protocol=upstream_protocol,
        client_surface=client_surface,
        canonical_request=canonical_request,
        reasoning_intent=(
            canonical_request.reasoning if canonical_request is not None else None
        ),
        transcode_required=True,
    )
    _features = getattr(transcoder_policy, "features", None)
    _transcoding_capability = None
    model_info = catalog.cache.get_model_for_provider(model_id, provider_id)
    if model_info is not None:
        from eggpool.catalog.capabilities import dict_to_model_capabilities

        _transcoding_capability = dict_to_model_capabilities(
            cast("dict[str, object]", model_info.get("capabilities", {})),
        ).transcoding
    # The preflight always runs the transcoder in ``warn`` mode so it
    # can collect the full warning list. The proxy layer below is
    # responsible for enforcing the operator's ``loss_policy`` after
    # translation completes.
    translated, warnings = transcoder.encode_request(
        payload,
        transcode_context,
        features=_features,
        transcoding_capability=_transcoding_capability,
        loss_policy="warn",
    )
    return TranscodePreflightResult(
        upstream_protocol=cast("ProtocolName", upstream_protocol),
        translated_payload=translated,
        warnings=warnings,
        tool_token_padding=_tool_token_padding(translated),
        canonical_request=canonical_request,
    )


def _format_loss_policy_rejection(warnings: list[dict[str, Any]]) -> str:
    """Build a bounded, diagnostic rejection message for lossy transcoding."""
    parts: list[str] = []
    for warning in warnings[:5]:
        field = warning.get("field")
        kind = warning.get("kind")
        if isinstance(field, str) and isinstance(kind, str):
            parts.append(f"{field} ({kind})")
        elif isinstance(field, str):
            parts.append(field)
        elif isinstance(kind, str):
            parts.append(kind)
    if len(warnings) > 5:
        parts.append(f"{len(warnings) - 5} more")
    detail = ", ".join(parts) if parts else "loss warnings were produced"
    return f"Request cannot be transcoded without losing information: {detail}"


async def handle_proxy_request(
    request: Request,
    endpoint: ProxyEndpointConfig,
) -> Response:
    """Validate and dispatch one OpenAI- or Anthropic-compatible request."""
    # Capture the earliest ASGI handler entry after auth / body-limit
    # middleware.  Stored on the request
    # state so ``_handle_proxy_request_inner`` can propagate it onto
    # the ``ProxyRequestContext`` and ``_send_upstream_request`` can
    # compute ``local_pre_upstream_ms`` from this anchor.
    request_received_monotonic_ns = time.perf_counter_ns()
    # Generate the proxy request ID early so it can be used for
    # request-coherent span sampling (Plan 029, Workstream H) before
    # any spans are recorded.
    proxy_request_id = str(uuid.uuid4())
    request_state = getattr(request, "state", None)
    if request_state is not None:
        with contextlib.suppress(AttributeError):
            # Some test doubles disallow attribute assignment; ignore.
            request_state.request_received_monotonic_ns = request_received_monotonic_ns
            request_state.proxy_request_id = proxy_request_id
    # Acquire a generation lease so the active generation cannot be
    # retired while this request is in flight.  For streaming responses
    # the lease is transferred to ``wrap_stream_with_lease`` which
    # releases it after the last chunk or on client disconnect.
    lease: GenerationLease | None = None
    runtime_manager = getattr(request.app.state, "runtime_manager", None)
    if runtime_manager is None:
        logger.error(
            "Proxy request rejected because RuntimeManager is not installed",
            extra={"proxy_request_id": proxy_request_id},
        )
        return endpoint.error_response(
            status_code=503,
            message="Runtime generation unavailable",
            error_type=endpoint.service_error_type,
        )
    try:
        lease = await runtime_manager.acquire()
    except Exception as exc:
        request_state = getattr(request, "state", None)
        request_id_or_none = (
            getattr(request_state, "proxy_request_id", None)
            if request_state is not None
            else None
        )
        logger.warning(
            "Runtime lease acquisition failed; returning 503",
            extra={
                "proxy_request_id": request_id_or_none,
                "error": repr(exc),
            },
        )
        return endpoint.error_response(
            status_code=503,
            message="Runtime generation unavailable",
            error_type=endpoint.service_error_type,
        )
    if lease is None:
        logger.error(
            "RuntimeManager returned no lease after successful acquisition",
            extra={"proxy_request_id": proxy_request_id},
        )
        return endpoint.error_response(
            status_code=503,
            message="Runtime generation unavailable",
            error_type=endpoint.service_error_type,
        )
    coordinator = lease.runtime.coordinator
    span_recorder = getattr(lease.runtime, "dispatch_span_recorder", None)
    model_router_affinity = getattr(
        getattr(request.app.state, "process", None),
        "model_router_affinity",
        None,
    )
    model_router_metrics = getattr(
        getattr(request.app.state, "process", None),
        "model_router_metrics",
        None,
    )

    # Plan 029, Workstream H: request-coherent span sampling.
    # The sampling decision is deterministic and stable per request ID
    # so that one sampled request records all spans (coherent trace).
    # If the request is not sampled, pass ``None`` as the span recorder
    # so all ``_span`` calls become no-ops.
    if span_recorder is not None and hasattr(span_recorder, "should_sample_request"):
        sampled = span_recorder.should_sample_request(proxy_request_id)  # type: ignore[union-attr]
        if not sampled:
            span_recorder = None

    try:
        return await _handle_proxy_request_inner(
            request,
            endpoint,
            coordinator,
            span_recorder,
            lease,
            model_router_affinity=model_router_affinity,
            model_router_metrics=model_router_metrics,
            request_received_monotonic_ns=(
                getattr(
                    getattr(request, "state", None),
                    "request_received_monotonic_ns",
                    None,
                )
                or request_received_monotonic_ns
            ),
            proxy_request_id=proxy_request_id,
        )
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        # Final request-level containment for faults outside the coordinator
        # stages (including parsing/admission defects).  The request ID is
        # safe to expose; request bodies, provider bodies, credentials, and
        # traceback text stay server-side.
        logger.exception(
            "Unhandled ordinary proxy request exception: request_id=%s "
            "protocol=%s exception=%s",
            proxy_request_id,
            endpoint.protocol,
            "ordinary_exception",
        )
        response = endpoint.error_response(
            status_code=500,
            message="Internal proxy error",
            error_type=endpoint.service_error_type,
        )
        response.headers["x-proxy-request-id"] = proxy_request_id
        return response
    finally:
        # For non-streaming error paths the lease is still held here.
        # Streaming success transfers the lease to wrap_stream_with_lease.
        if not lease.released:
            await lease.release()


async def _handle_proxy_request_inner(
    request: Request,
    endpoint: ProxyEndpointConfig,
    coordinator: RequestCoordinator,
    span_recorder: DispatchSpanRecorder | None,
    lease: GenerationLease,
    *,
    model_router_affinity: ModelRouterAffinity | None = None,
    model_router_metrics: Any | None = None,
    request_received_monotonic_ns: int | None = None,
    proxy_request_id: str | None = None,
) -> Response:
    """Inner handler body; called within the lease's try/finally."""
    with _span(span_recorder, SPAN_AUTH):
        await require_auth(request)

    try:
        with _span(span_recorder, SPAN_BODY_READ):
            body = await read_body_limited(
                request, lease.runtime.config.server.max_request_body_bytes
            )
    except RequestTooLargeError:
        return endpoint.error_response(
            status_code=413,
            message="Request body too large",
            error_type="invalid_request_error",
        )

    with _span(span_recorder, SPAN_JSON_PARSE):
        payload_obj: object
        try:
            payload_obj = jsonx_loads(body)
        except ValueError:
            return endpoint.error_response(
                status_code=400,
                message="Invalid JSON",
                error_type="invalid_request_error",
            )
        if not isinstance(payload_obj, dict):
            return endpoint.error_response(
                status_code=400,
                message="Invalid JSON",
                error_type="invalid_request_error",
            )
    payload = cast("dict[str, Any]", payload_obj)

    # F7: create parsed-payload cache once, seed with the already-parsed
    # dict so downstream accesses skip json.loads entirely.
    parsed_payload = ParsedRequestPayload(original_bytes=body)
    parsed_payload._parsed_dict = payload  # type: ignore[assignment]

    model_value = payload.get("model")
    if not isinstance(model_value, str) or not model_value.strip():
        return endpoint.error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )

    # Responses is intentionally stateless. Reject stateful fields before a
    # virtual router can spend selector work or create a child request.
    if endpoint.request_surface == "responses":
        rejection = _validate_responses_stateless(payload)
        if rejection is not None:
            return endpoint.error_response(
                status_code=400,
                message=rejection,
                error_type="invalid_request_error",
            )

    client_surface = _canonical_surface_for_endpoint(endpoint)
    # Provider id parsing relies on the leased generation's precomputed
    # provider set (built from the registry when the generation was
    # constructed).  Reading through ``request.app.state.config`` would
    # bypass the lease and use a generation that may already be retired.
    known_providers = lease.runtime.immutable_request_state.provider_ids
    model_id = model_value
    provider_id: str | None = None
    registry = getattr(lease.runtime, "model_router_registry", None)
    router = (
        registry.get(model_value) if isinstance(registry, ModelRouterRegistry) else None
    )

    if router is None:
        # Keep the feature-off concrete path in its original order and shape.
        with _span(span_recorder, SPAN_MODEL_PARSE):
            model_id, provider_id = parse_model_provider(model_value, known_providers)
        try:
            canonical_request = canonical_request_from_mapping(
                payload,
                client_surface=client_surface,
                protocol=endpoint.protocol,
            )
        except ValueError as exc:
            return endpoint.error_response(
                status_code=400,
                message=str(exc),
                error_type="invalid_request_error",
            )
    else:
        # The virtual branch is the only path that builds an early canonical
        # view. It resolves the exact alias before concrete parsing and all
        # target-specific capability/context/transcode checks.
        try:
            canonical_request = canonical_request_from_mapping(
                payload,
                client_surface=client_surface,
                protocol=endpoint.protocol,
            )
        except ValueError as exc:
            return endpoint.error_response(
                status_code=400,
                message=str(exc),
                error_type="invalid_request_error",
            )

    router_metadata: dict[str, Any] = {}
    if router is not None:
        semantic_started = time.perf_counter()
        selector = ModelRouterSelector(
            coordinator,
            known_provider_ids=known_providers,
        )
        identity = None
        session_header = request.headers.get(AFFINITY_SESSION_HEADER)
        if session_header is not None:
            identity = session_identity_from_header(session_header)
        else:
            identity = automatic_session_identity(
                canonical_request,
                client_surface=client_surface,
            )

        if (
            router.sticky
            and identity is not None
            and isinstance(model_router_affinity, ModelRouterAffinity)
        ):
            resolution = await model_router_affinity.resolve(
                router,
                identity,
                lambda: selector.select(
                    router,
                    payload,
                    client_surface=client_surface,
                    protocol=endpoint.protocol,
                ),
            )
            selected_model = resolution.decision.concrete_model
            route_id = resolution.decision.route_id
            route_label = resolution.decision.route_label
            source = resolution.decision.source
            if resolution.cache_hit or resolution.single_flight_join:
                # No new selector inference ran for this request (cache hit
                # or single-flight follower), so repair/fallback work belongs
                # to the leader's own request and must not be recounted here.
                selector_attempts = 0
                fallback_reason = None
                repair_attempted = False
                repair_succeeded = False
            else:
                # Sticky miss: the leader just ran the selector for this
                # request; attribute its bounded attempt/repair outcome.
                selector_attempts = resolution.decision.selector_attempts
                fallback_reason = resolution.decision.fallback_reason
                repair_attempted = resolution.decision.repair_attempted
                repair_succeeded = resolution.decision.repair_succeeded
            router_metadata = {
                "virtual_model": router.virtual_model,
                "route_id": route_id,
                "route_label": route_label,
                "decision_source": source,
                "affinity_hit": resolution.cache_hit,
                "session_source": identity.source,
            }
        else:
            selected = await selector.select(
                router,
                payload,
                client_surface=client_surface,
                protocol=endpoint.protocol,
            )
            selected_model = selected.concrete_model
            source = selected.source
            selector_attempts = selected.selector_attempts
            fallback_reason = selected.fallback_reason
            repair_attempted = selected.repair_attempted
            repair_succeeded = selected.repair_succeeded
            router_metadata = {
                "virtual_model": router.virtual_model,
                "route_id": selected.route_id,
                "route_label": selected.route_label,
                "decision_source": selected.source,
                "affinity_hit": False,
                "session_source": (
                    identity.source if identity is not None else "no_session"
                ),
            }
        if model_router_metrics is not None and hasattr(
            model_router_metrics, "record_resolution"
        ):
            model_router_metrics.record_resolution(
                virtual_model=router.virtual_model,
                concrete_model=selected_model,
                source=source,
                affinity_hit=bool(router_metadata.get("affinity_hit", False)),
                selector_attempts=selector_attempts,
                fallback_reason=fallback_reason,
                repair_attempted=repair_attempted,
                repair_succeeded=repair_succeeded,
                latency_ms=(time.perf_counter() - semantic_started) * 1000.0,
            )
        payload = dict(payload)
        payload["model"] = selected_model
        with _span(span_recorder, SPAN_MODEL_PARSE):
            model_id, provider_id = parse_model_provider(
                selected_model, known_providers
            )
        try:
            canonical_request = canonical_request_from_mapping(
                payload,
                client_surface=client_surface,
                protocol=endpoint.protocol,
            )
        except ValueError as exc:
            return endpoint.error_response(
                status_code=400,
                message=str(exc),
                error_type="invalid_request_error",
            )

    # Normalize the payload's model field to the parsed model_id BEFORE the
    # transcode preflight runs. The preflight caches the translated payload
    # and a JSON-encoded body so the coordinator can skip a duplicate encode
    # on the hot path. If the cached body still carries a provider-suffixed
    # ``model-id/provider-id`` value (which is how ``/v1/models`` exposes
    # provider-scoped entries), the upstream receives the suffix and rejects
    # the request with ``Model <model-id>/<provider-id> is not supported``
    # because the upstream does not understand eggpool's namespace. Rewriting
    # the in-memory snapshot here keeps both the preflight encoded body and
    # the provider-bound payload consistent.
    preflight_payload = payload
    if model_id != model_value:
        preflight_payload = dict(payload)
        preflight_payload["model"] = model_id

    # Capture semantic client intent once, before any provider-specific
    # encoder can rewrite reasoning controls.  Retries and alternate target
    # surfaces must start from this canonical request, never from a prior
    # provider payload generation.
    # Preflight context limit check (guardrail, not primary enforcement).
    catalog = lease.runtime.catalog
    transcoder_policy = lease.runtime.transcoder_policy
    preflight: TranscodePreflightResult | None = None
    prepared_transcode: PreparedTranscode | None = None
    precomputed_context_input_tokens: int | None = None
    with _span(span_recorder, SPAN_CONTEXT_LIMIT):
        try:
            precomputed_context_input_tokens = _check_context_limits(
                model_id=model_id,
                provider_id=provider_id,
                body=body,
                payload=payload,
                protocol=endpoint.protocol,
                catalog_cache=catalog.cache,
                request_surface=endpoint.request_surface,
            )
        except ContextLimitExceededError as exc:
            return endpoint.error_response(
                status_code=400,
                message=str(exc),
                error_type="invalid_request_error",
            )

    # Second pass: when legacy transcoding is active, also validate the
    # translated payload against upstream limits. Responses skips that
    # preflight, but alternate wire profiles are encoded from canonical intent
    # after provider selection.
    with _span(span_recorder, SPAN_TRANSCODE_PREFLIGHT):
        if endpoint.request_surface == "responses":
            preflight = None
            prepared_transcode = None
        else:
            preflight = _prepare_transcode_preflight(
                catalog=catalog,
                model_id=model_id,
                provider_id=provider_id,
                client_protocol=endpoint.protocol,
                payload=preflight_payload,
                transcoder_policy=transcoder_policy,
                client_surface=client_surface,
                canonical_request=canonical_request,
            )
            if preflight is not None:
                if (
                    getattr(transcoder_policy, "loss_policy", "warn") == "reject"
                    and preflight.warnings
                ):
                    return endpoint.error_response(
                        status_code=400,
                        message=_format_loss_policy_rejection(preflight.warnings),
                        error_type="invalid_request_error",
                    )
                try:
                    encoded_translated_body = encode_json_body(
                        preflight.translated_payload,
                    )
                except ValueError:
                    return endpoint.error_response(
                        status_code=400,
                        message="Invalid JSON",
                        error_type="invalid_request_error",
                    )
                try:
                    _check_context_limits(
                        model_id=model_id,
                        provider_id=provider_id,
                        body=encoded_translated_body,
                        payload=preflight.translated_payload,
                        protocol=preflight.upstream_protocol,
                        catalog_cache=catalog.cache,
                        extra_input_tokens=preflight.tool_token_padding,
                        request_surface=endpoint.request_surface,
                    )
                except ContextLimitExceededError as exc:
                    return endpoint.error_response(
                        status_code=400,
                        message=str(exc),
                        error_type="invalid_request_error",
                    )
                _loss_policy = getattr(transcoder_policy, "loss_policy", "warn")
                _features = getattr(transcoder_policy, "features", None)
                # Plan 141: when the request carries provider-sensitive
                # multimodal content, the preflight translation cannot be
                # safely reused across providers with different source-form
                # contracts. Skip caching the ``PreparedTranscode`` so the
                # coordinator forces a final recompute against the *selected*
                # provider's capability row after ``SelectedAttempt`` exists.
                from eggpool.transcoder.sensitive_media import (
                    request_has_provider_sensitive_media,
                )

                _has_provider_sensitive_media = request_has_provider_sensitive_media(
                    payload
                )
                if not _has_provider_sensitive_media:
                    prepared_transcode = PreparedTranscode.from_preflight_result(
                        result=preflight,
                        client_protocol=endpoint.protocol,
                        loss_policy=_loss_policy,
                        encoded_body=encoded_translated_body,
                        features=_features,
                    )

    stream_value = payload.get("stream", False)
    if stream_value is not None and not isinstance(stream_value, bool):
        return endpoint.error_response(
            status_code=400,
            message="Invalid stream value: must be a boolean",
            error_type="invalid_request_error",
        )
    is_stream = bool(stream_value)

    request_id = proxy_request_id or str(uuid.uuid4())
    transcode_ctx = TranscodeContext(
        request_id=request_id,
        client_protocol=endpoint.protocol,
        upstream_protocol=endpoint.protocol,
        client_surface=client_surface,
        selected_wire_surface=(
            "anthropic_messages"
            if endpoint.protocol == "anthropic"
            else "openai_responses"
            if endpoint.request_surface == "responses"
            else "openai_chat_completions"
        ),
        canonical_request=canonical_request,
        reasoning_intent=canonical_request.reasoning,
    )

    # Semantic compression has been removed.  Segmentation and
    # compression are no longer performed; the request body is
    # passed through unchanged.
    segmentation_result: Any = None
    segmentation_not_collected = True

    # Phase 5: precompute thinking requirement and reservation tokens so the
    # coordinator does not have to reparse ``original_body`` (and re-classify)
    # inside the selection claim.  The canonical context estimate, when
    # needed, was returned by the limit check above and is already carried in
    # ``precomputed_context_input_tokens``. These computations are pure
    # functions of the body and client protocol — they read no mutable runtime
    # state and therefore do not need serialization against other requests.
    precomputed_thinking_req: Any = None
    precomputed_reservation_tokens: int | None = None
    # The context-limit check above returns the same canonical estimate used
    # for request context.  An unbounded/no-enforcement model leaves this as
    # ``None`` because coordinator admission only needs the reservation
    # estimate in that case.
    precomputed_thinking_req = classify_thinking_request(
        cast("dict[str, object]", payload),
        endpoint.protocol,
    )
    precomputed_reservation_tokens = estimate_reservation_tokens(body)
    with _span(span_recorder, SPAN_CONTEXT_BUILD):
        # Plan 028: create a typed provider-bound lifecycle object so
        # the coordinator and downstream consumers read from a single
        # authoritative decoded payload instead of re-parsing bytes.
        provider_bound = ProviderBoundRequest(
            client_bytes=body,
            client_payload=payload,
            client_protocol=endpoint.protocol,
            model_id=model_id,
        )
        # The rewrite is provider-bound state, while ``client_payload``
        # remains the immutable parsed client snapshot. Keep the normalized
        # model in the decoded object so final serialization cannot fall back
        # to the client-suffixed model.
        provider_payload = dict(payload)
        if provider_payload.get("model") != model_id:
            provider_payload["model"] = model_id
        if provider_payload != payload:
            provider_bound.set_provider_payload(
                provider_payload, increment_generation=False
            )

        context = ProxyRequestContext(
            request_id=request_id,
            protocol=endpoint.protocol,
            model_id=model_id,
            streaming=is_stream,
            original_body=body,
            incoming_headers=dict(request.headers),
            started_at=time.time(),
            # Propagate the ASGI handler-entry anchor so
            # ``_send_upstream_request`` can compute ``local_pre_upstream_ms``.
            request_received_monotonic_ns=request_received_monotonic_ns,
            provider_id=provider_id,
            client_ip=get_client_ip(
                request,
                trusted_proxies=lease.runtime.immutable_request_state.trusted_proxies,
            ),
            upstream_protocol=endpoint.protocol,
            request_surface=endpoint.request_surface,
            client_surface=client_surface,
            selected_wire_surface=transcode_ctx.selected_wire_surface,
            canonical_request=canonical_request,
            reasoning_intent=canonical_request.reasoning,
            transcode_required=False,
            transcode_context=transcode_ctx,
            segmentation=segmentation_result,
            segmentation_not_collected=segmentation_not_collected,
            prepared_transcode=prepared_transcode,
            estimated_reservation_tokens=precomputed_reservation_tokens,
            thinking_requirement=precomputed_thinking_req,
            estimated_context_input_tokens=precomputed_context_input_tokens,
            parsed_payload=parsed_payload,
            provider_bound=provider_bound,
            client_metadata=(
                {"model_router": router_metadata} if router_metadata else {}
            ),
        )

    logger.debug(
        "Proxying %s: model=%s proxy_request_id=%s streaming=%s",
        endpoint.request_label,
        model_value,
        context.request_id,
        is_stream,
    )

    try:
        result = await coordinator.execute(context)
    except RequestTooLargeError:
        # Provider-bound serialized-size rejection (HTTP 413). Distinct
        # from the ingress ``read_body_limited()`` 413 path: this is a
        # local client-validation failure observed after provider
        # selection/translation. The durable attempt is already
        # terminalized through the canonical owner; the API handler
        # only renders the bounded client-facing error.
        return endpoint.error_response(
            status_code=413,
            message="Serialized request body too large",
            error_type="invalid_request_error",
        )
    except ModelNotFoundError as exc:
        return endpoint.error_response(
            status_code=404,
            message=str(exc),
            error_type=endpoint.not_found_error_type,
        )
    except CapabilityError as exc:
        renderer = (
            anthropic_capability_error_response
            if endpoint.protocol == "anthropic"
            else openai_capability_error_response
        )
        return renderer(
            status_code=400,
            message=str(exc),
            capability=exc.capability,
            requested_fields=exc.requested_fields,
            model=exc.model_id,
        )
    except TranscodeLossError as exc:
        return endpoint.error_response(
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
        )
    except (
        NoEligibleAccountError,
        CatalogUnavailableError,
        ModelUnavailableError,
        AccountSuspendedError,
    ) as exc:
        return endpoint.error_response(
            status_code=503,
            message=str(exc),
            error_type=endpoint.service_error_type,
        )
    except UpstreamExhaustedError as exc:
        return endpoint.error_response(
            status_code=502,
            message=str(exc),
            error_type=endpoint.service_error_type,
        )
    except ProtocolMismatchError as exc:
        return endpoint.error_response(
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
        )

    response = render_proxy_response(result)

    # For streaming responses, transfer the generation lease to the
    # stream wrapper so it outlives the handler return.  The wrapper
    # releases the lease on stream completion, client disconnect, or
    # error — whichever happens first.
    if result.stream_iterator is not None and isinstance(
        response, ProxyStreamingResponse
    ):
        response = ProxyStreamingResponse(
            wrap_stream_with_lease(result.stream_iterator, lease),
            status_code=response.status_code,
            media_type=response.media_type,
            headers=response.headers,
            response_handoff=response.response_handoff,
        )
    else:
        # Non-streaming: release immediately since finalization is done.
        await lease.release()

    return response


@contextlib.contextmanager
def _span(
    recorder: DispatchSpanRecorder | None,
    name: str,
) -> typing.Generator[None, None, None]:
    """No-op context manager when no recorder is registered.

    Keeps the hot path branch-free so callers can wrap suspect
    regions without measuring span scaffolding cost; the cost of a
    missing recorder collapses to ``None.__enter__`` which is a
    no-op.
    """
    if recorder is None:
        yield
        return
    timer = recorder.measure(name)
    timer.__enter__()
    try:
        yield
    except BaseException:
        # Cancellation is a BaseException; always close the timer before
        # propagating cancellation or another fatal exception.
        timer.__exit__(*sys.exc_info())
        raise
    timer.__exit__(None, None, None)
