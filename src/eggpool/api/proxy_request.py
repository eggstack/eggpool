"""Shared request handling for protocol-compatible proxy endpoints."""

from __future__ import annotations

import contextlib
import logging
import sys
import time
import typing
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi.responses import JSONResponse, Response, StreamingResponse

from eggpool.api.errors import (
    anthropic_capability_error_response,
    openai_capability_error_response,
)
from eggpool.auth import require_auth
from eggpool.catalog.capabilities import classify_thinking_request
from eggpool.catalog.protocols import ProtocolMismatchError, ProtocolName
from eggpool.constants import MAX_REQUEST_BODY_BYTES
from eggpool.errors import (
    CapabilityError,
    CatalogUnavailableError,
    ContextLimitExceededError,
    ModelNotFoundError,
    ModelUnavailableError,
    NoEligibleAccountError,
    RequestTooLargeError,
    UpstreamExhaustedError,
)
from eggpool.jsonx import dumps_bytes
from eggpool.jsonx import loads as jsonx_loads
from eggpool.request.body import encode_json_body, read_body_limited
from eggpool.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.request.limits import (
    ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR,
    estimate_context_input_tokens,
    estimate_reservation_tokens,
)
from eggpool.request.limits import (
    check_context_limits as _check_context_limits,
)
from eggpool.routing.provider import parse_model_provider
from eggpool.runtime_dispatch import (
    SPAN_AUTH,
    SPAN_BODY_READ,
    SPAN_COMPRESSION_ANALYZE,
    SPAN_COMPRESSION_APPLY,
    SPAN_COMPRESSION_POLICY,
    SPAN_CONTEXT_BUILD,
    SPAN_CONTEXT_LIMIT,
    SPAN_JSON_PARSE,
    SPAN_MODEL_PARSE,
    SPAN_SEGMENTATION,
    SPAN_TRANSCODE_PREFLIGHT,
    DispatchSpanRecorder,
)
from eggpool.runtime_manager import GenerationLease, wrap_stream_with_lease
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.prepared import PreparedTranscode
from eggpool.transcoder.segmentation_guard import should_segment_request

if TYPE_CHECKING:
    from fastapi import Request

    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)


class ErrorResponseFactory(Protocol):
    """Callable contract implemented by protocol-specific error renderers."""

    def __call__(
        self,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
    ) -> JSONResponse: ...


@dataclass(frozen=True)
class ProxyEndpointConfig:
    """Protocol-specific behavior for the shared proxy endpoint pipeline."""

    protocol: ProtocolName
    request_label: str
    error_response: ErrorResponseFactory
    not_found_error_type: str
    service_error_type: str


@dataclass(frozen=True)
class TranscodePreflightResult:
    """Result of translating a request body before durable dispatch."""

    upstream_protocol: ProtocolName
    translated_payload: dict[str, Any]
    warnings: list[dict[str, Any]]
    tool_token_padding: int = 0


def _tool_token_padding(payload: dict[str, Any]) -> int:
    """Estimate extra input tokens from tool schemas in a translated payload.

    Anthropic tool schemas (``input_schema``) are typically 30 % of their
    JSON size in tokens.  The padding is conservative enough to avoid
    false rejections without inflating reservations excessively.
    """
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0
    total_bytes = 0
    tool_list = cast("list[dict[str, Any]]", tools)
    for tool in tool_list:
        total_bytes += len(dumps_bytes(tool))
    return max(64, total_bytes // 4)


def get_client_ip(request: Request) -> str:
    """Extract the reported client IP, accounting for reverse proxies."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host
    return ""


def render_proxy_response(result: PreparedProxyResponse) -> Response:
    """Render a prepared response without re-encoding its body or headers."""
    if result.stream_iterator is not None:
        response: Response = StreamingResponse(
            result.stream_iterator,
            status_code=result.status_code,
            media_type=None,
        )
    else:
        response = Response(
            content=result.body,
            status_code=result.status_code,
            media_type=None,
        )

    for name, value in result.headers:
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
    )
    _features = getattr(transcoder_policy, "features", None)
    # The preflight always runs the transcoder in ``warn`` mode so it
    # can collect the full warning list. The proxy layer below is
    # responsible for enforcing the operator's ``loss_policy`` after
    # translation completes.
    translated, warnings = transcoder.encode_request(
        payload,
        transcode_context,
        features=_features,
        loss_policy="warn",
    )
    return TranscodePreflightResult(
        upstream_protocol=cast("ProtocolName", upstream_protocol),
        translated_payload=translated,
        warnings=warnings,
        tool_token_padding=_tool_token_padding(translated),
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
    # Milestone A4 timing boundary: capture the earliest ASGI handler
    # entry after auth / body-limit middleware.  Stored on the request
    # state so ``_handle_proxy_request_inner`` can propagate it onto
    # the ``ProxyRequestContext`` and ``_send_upstream_request`` can
    # compute ``local_pre_upstream_ms`` from this anchor.
    request_received_monotonic_ns = time.perf_counter_ns()
    request_state = getattr(request, "state", None)
    if request_state is not None:
        with contextlib.suppress(AttributeError):
            # Some test doubles disallow attribute assignment; ignore.
            request_state.request_received_monotonic_ns = request_received_monotonic_ns
    # Acquire a generation lease so the active generation cannot be
    # retired while this request is in flight.  For streaming responses
    # the lease is transferred to ``wrap_stream_with_lease`` which
    # releases it after the last chunk or on client disconnect.
    lease: GenerationLease | None = None
    runtime_manager = getattr(request.app.state, "runtime_manager", None)
    if runtime_manager is not None:
        try:
            lease = await runtime_manager.acquire()
        except Exception:  # noqa: BLE001 — fall back to app.state
            lease = None

    if lease is not None:
        coordinator = lease.runtime.coordinator
        span_recorder = getattr(lease.runtime, "dispatch_span_recorder", None)
    else:
        coordinator = cast("RequestCoordinator", request.app.state.coordinator)
        span_recorder = cast(
            "DispatchSpanRecorder | None",
            getattr(request.app.state, "dispatch_span_recorder", None),
        )

    try:
        return await _handle_proxy_request_inner(
            request,
            endpoint,
            coordinator,
            span_recorder,
            lease,
            request_received_monotonic_ns=(
                getattr(
                    getattr(request, "state", None),
                    "request_received_monotonic_ns",
                    None,
                )
                or request_received_monotonic_ns
            ),
        )
    finally:
        # For non-streaming error paths the lease is still held here.
        # Streaming success transfers the lease to wrap_stream_with_lease.
        if lease is not None and not lease.released:
            await lease.release()


async def _handle_proxy_request_inner(
    request: Request,
    endpoint: ProxyEndpointConfig,
    coordinator: RequestCoordinator,
    span_recorder: DispatchSpanRecorder | None,
    lease: GenerationLease | None,
    *,
    request_received_monotonic_ns: int | None = None,
) -> Response:
    """Inner handler body; called within the lease's try/finally."""
    with _span(span_recorder, SPAN_AUTH):
        await require_auth(request)

    try:
        with _span(span_recorder, SPAN_BODY_READ):
            body = await read_body_limited(request, MAX_REQUEST_BODY_BYTES)
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

    model_value = payload.get("model")
    if not isinstance(model_value, str) or not model_value.strip():
        return endpoint.error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )

    config = cast("AppConfig | None", getattr(request.app.state, "config", None))
    known_providers = set(config.providers) if config is not None else None
    with _span(span_recorder, SPAN_MODEL_PARSE):
        model_id, provider_id = parse_model_provider(model_value, known_providers)

    # Preflight context limit check (guardrail, not primary enforcement).
    catalog = getattr(request.app.state, "catalog", None)
    preflight: TranscodePreflightResult | None = None
    prepared_transcode: PreparedTranscode | None = None
    transcoder_policy = getattr(request.app.state, "transcoder_policy", None)
    if catalog is not None:
        with _span(span_recorder, SPAN_CONTEXT_LIMIT):
            try:
                _check_context_limits(
                    model_id=model_id,
                    provider_id=provider_id,
                    body=body,
                    payload=payload,
                    protocol=endpoint.protocol,
                    catalog_cache=catalog.cache,
                )
            except ContextLimitExceededError as exc:
                return endpoint.error_response(
                    status_code=400,
                    message=str(exc),
                    error_type="invalid_request_error",
                )

        # Second pass: when transcoding is active, also validate
        # the translated payload against upstream limits.
        with _span(span_recorder, SPAN_TRANSCODE_PREFLIGHT):
            preflight = _prepare_transcode_preflight(
                catalog=catalog,
                model_id=model_id,
                provider_id=provider_id,
                client_protocol=endpoint.protocol,
                payload=payload,
                transcoder_policy=transcoder_policy,
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
                    limit_check_body = encoded_translated_body
                    if preflight.tool_token_padding > 0:
                        limit_check_body += b"\x00" * (
                            preflight.tool_token_padding
                            * ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR
                        )
                    _check_context_limits(
                        model_id=model_id,
                        provider_id=provider_id,
                        body=limit_check_body,
                        payload=preflight.translated_payload,
                        protocol=preflight.upstream_protocol,
                        catalog_cache=catalog.cache,
                    )
                except ContextLimitExceededError as exc:
                    return endpoint.error_response(
                        status_code=400,
                        message=str(exc),
                        error_type="invalid_request_error",
                    )
                _loss_policy = getattr(transcoder_policy, "loss_policy", "warn")
                _features = getattr(transcoder_policy, "features", None)
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

    request_id = str(uuid.uuid4())
    transcode_ctx = TranscodeContext(
        request_id=request_id,
        client_protocol=endpoint.protocol,
        upstream_protocol=endpoint.protocol,
    )

    # Phase 6: resolve the compression policy for this request.
    # Resolution merges the global ``[compression]`` config with any
    # matching ``[[compression.policies]]`` entries.  The resolver is
    # content-private (it never inspects the request body) and
    # fail-closed: a malformed override logs a warning and falls
    # back to the global config.  The resolved config is what the
    # analyzer, the applier, and the finalizer all see, so observe
    # mode and safe mode always agree on the per-request knobs.
    #
    # Resolution happens pre-route.  Provider id / kind / resolved
    # model are not yet known, so provider-specific overrides are
    # silently skipped pre-route; operators who need provider-
    # specific policy must do a second post-route pass (or rely on
    # the broader client / protocol / model match fields).
    #
    # This runs BEFORE the segmentation guard so the guard reads the
    # effective (possibly policy-overridden) compression enabled/mode
    # instead of the raw global config.
    compression_policy = getattr(request.app.state, "compression_policy", None)
    runtime_override_registry: Any = getattr(
        request.app.state,
        "compression_tuning_registry",
        None,
    )
    with _span(span_recorder, SPAN_COMPRESSION_POLICY):
        resolved_compression_policy: Any = None
        if compression_policy is not None:
            try:
                from eggpool.transcoder.compression import (
                    CompressionPolicyContext,
                    resolve_compression_policy,
                )

                policy_ctx = CompressionPolicyContext(
                    client_id=request.headers.get("x-eggpool-client"),
                    client_name=request.headers.get("user-agent"),
                    source_protocol=endpoint.protocol,
                    target_protocol=endpoint.protocol,
                    requested_model=model_value,
                    resolved_model=None,
                    provider_id=None,
                    provider_kind=None,
                    transcoded=False,
                )
                resolved_compression_policy = resolve_compression_policy(
                    compression_policy,
                    policy_ctx,
                    runtime_override_registry=runtime_override_registry,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "compression_policy_resolution_failed",
                    extra={"proxy_request_id": request_id},
                    exc_info=True,
                )
                resolved_compression_policy = None

    # The analyzer and applier read the resolved config when
    # available; fall back to the global config when resolution
    # failed (the resolver itself is fail-closed, but the import or
    # the call could still raise on malformed state).
    effective_compression_policy: Any = (
        resolved_compression_policy.config
        if resolved_compression_policy is not None
        else compression_policy
    )

    # Phase 2.1 (performance optimization): segmentation is skipped
    # when no consumer needs it — compression observe/safe, synthetic
    # cache controls, or cache observability.  The guard checks the
    # effective compression policy resolved above rather than the
    # raw global config, so a scoped ``[[compression.policies]]``
    # override that enables observe/safe is correctly detected.
    _cache_cfg = getattr(config, "cache", None) if config is not None else None
    _synthetic_enabled = (
        getattr(
            getattr(_cache_cfg, "synthetic_cache_controls", None),
            "enabled",
            False,
        )
        if _cache_cfg is not None
        else False
    )
    _seg_compression_enabled = (
        getattr(effective_compression_policy, "enabled", False)
        if effective_compression_policy is not None
        else False
    )
    _seg_compression_mode = (
        str(getattr(effective_compression_policy, "mode", "off"))
        if effective_compression_policy is not None
        else "off"
    )
    _segmentation_needed = should_segment_request(
        config,
        compression_enabled=_seg_compression_enabled,
        compression_mode=_seg_compression_mode,
        synthetic_cache_enabled=_synthetic_enabled,
        cache_observability_enabled=False,
        force_segmentation=getattr(config, "force_segmentation", False)
        if config is not None
        else False,
    )

    segmentation_result: Any = None
    segmentation_not_collected = False
    if _segmentation_needed:
        with _span(span_recorder, SPAN_SEGMENTATION):
            try:
                from eggpool.transcoder.segmentation import segment_request

                segmentation_result = segment_request(
                    payload, protocol=endpoint.protocol
                )
            except Exception:  # noqa: BLE001
                # Segmentation is observational.  A failure here must never
                # block the request path; the finalizer falls back to
                # ``segmentation_status = 'empty_request'``.
                logger.debug(
                    "segmentation_failed",
                    extra={"proxy_request_id": request_id},
                    exc_info=True,
                )
                segmentation_result = None
    else:
        segmentation_not_collected = True

    # Phase 4: run the observe-mode compression analyzer.  The
    # analyzer is observational and never mutates the request
    # body.  It runs only when ``[compression] enabled = true``;
    # otherwise it short-circuits to ``None`` and the finalizer
    # records no compression fields.  Failure here must never
    # block the request path.
    #
    # When ``mode == "safe"`` the analyzer is skipped entirely; the
    # safe-mode applier builds an equivalent observation from its
    # own pass so we don't run two full compression walks for the
    # same request.  The finalizer duck-types against the
    # ``CompressionObservation`` shape, so the safe-mode adapter
    # exposed by ``build_safe_mode_observation`` covers the same
    # fields without requiring an independent analyzer call.
    compression_observation: Any = None
    if (
        effective_compression_policy is not None
        and getattr(effective_compression_policy, "enabled", False)
        and getattr(effective_compression_policy, "mode", None) == "observe"
    ):
        with _span(span_recorder, SPAN_COMPRESSION_ANALYZE):
            try:
                from eggpool.transcoder.compression import analyze_compression

                compression_observation = analyze_compression(
                    segmentation_result,
                    policy=effective_compression_policy,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "compression_analysis_failed",
                    extra={"proxy_request_id": request_id},
                    exc_info=True,
                )
                compression_observation = None

    # Phase 5: run the safe-mode deterministic compressor.  The
    # applier mutates only eligible volatile_suffix segments,
    # applying transforms through path-level copy-on-write (no-op
    # runs return the original payload by identity, applied runs
    # copy only the dict/list ancestors on mutated paths and
    # preserve unchanged subtrees by reference); stable prefixes
    # and cache-protected blocks are never touched.  Runs only when
    # ``[compression] enabled = true`` AND ``[compression] mode =
    # 'safe'``; otherwise ``compression_result`` stays ``None`` and
    # the finalizer records safe defaults.  Failure here must never
    # block the request path.
    compression_result: Any = None
    if (
        effective_compression_policy is not None
        and getattr(effective_compression_policy, "enabled", False)
        and getattr(effective_compression_policy, "mode", None) == "safe"
        and segmentation_result is not None
    ):
        with _span(span_recorder, SPAN_COMPRESSION_APPLY):
            try:
                from eggpool.transcoder.compression.apply import (
                    apply_safe_compression,
                    build_safe_mode_observation,
                )

                compression_result = apply_safe_compression(
                    payload=payload,
                    segmentation=segmentation_result,
                    policy=effective_compression_policy,
                    text_hints=None,  # production is content-private
                )
                # Derive the observation from a single safe-mode pass
                # rather than running the analyzer separately (Phase 2
                # dispatch optimization).  ``compression_observation``
                # stays ``None`` when the applier fails so we don't
                # synthesize an observation that disagrees with the
                # request path's actual behavior.
                compression_observation = build_safe_mode_observation(
                    compression_result
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "compression_apply_failed",
                    extra={"proxy_request_id": request_id},
                    exc_info=True,
                )
                compression_result = None
                compression_observation = None

    # Determine the input payload for model rewrite: when Phase 5
    # compression applied transforms, use the mutated payload;
    # otherwise use the original client payload unchanged.
    payload_for_rewrite: dict[str, Any] = payload
    if compression_result is not None and getattr(compression_result, "applied", False):
        transformed = getattr(compression_result, "transformed_payload", None)
        if isinstance(transformed, dict):
            payload_for_rewrite = cast("dict[str, Any]", transformed)

    # Phase 9 synthetic cache control is now applied post-route inside
    # RequestCoordinator._apply_synthetic_cache_controls() so it operates on
    # the provider-bound payload with full upstream protocol context.
    synthetic_cache_result: Any = None

    # Phase 5: precompute thinking requirement, reservation tokens, and
    # context-input tokens once here so the coordinator does not have to
    # reparse ``original_body`` (and re-classify) inside ``_select_lock``.
    # These computations are pure functions of the body and the client
    # protocol — they read no mutable runtime state and therefore do not
    # need to be serialized against other concurrent requests.
    precomputed_thinking_req: Any = None
    precomputed_reservation_tokens: int | None = None
    precomputed_context_input_tokens: int | None = None
    precomputed_thinking_req = classify_thinking_request(
        cast("dict[str, object]", payload),
        endpoint.protocol,
    )
    precomputed_reservation_tokens = estimate_reservation_tokens(body)
    precomputed_context_input_tokens = estimate_context_input_tokens(body, payload)

    with _span(span_recorder, SPAN_CONTEXT_BUILD):
        context = ProxyRequestContext(
            request_id=request_id,
            protocol=endpoint.protocol,
            model_id=model_id,
            streaming=is_stream,
            original_body=body,
            incoming_headers=dict(request.headers),
            started_at=time.time(),
            # Milestone A4: propagate the ASGI handler-entry anchor so
            # ``_send_upstream_request`` can compute ``local_pre_upstream_ms``.
            request_received_monotonic_ns=request_received_monotonic_ns,
            provider_id=provider_id,
            client_ip=get_client_ip(request),
            upstream_body=_rewrite_upstream_model(payload_for_rewrite, model_id),
            upstream_protocol=endpoint.protocol,
            transcode_required=False,
            transcode_context=transcode_ctx,
            segmentation=segmentation_result,
            segmentation_not_collected=segmentation_not_collected,
            compression_observation=compression_observation,
            compression_result=compression_result,
            resolved_compression_policy=resolved_compression_policy,
            synthetic_cache_result=synthetic_cache_result,
            prepared_transcode=prepared_transcode,
            estimated_reservation_tokens=precomputed_reservation_tokens,
            thinking_requirement=precomputed_thinking_req,
            estimated_context_input_tokens=precomputed_context_input_tokens,
        )

    if segmentation_result is not None:
        logger.debug(
            "request_segmented",
            extra={
                "proxy_request_id": request_id,
                "model": model_id,
                "protocol": endpoint.protocol,
                "segmentation_status": str(
                    getattr(segmentation_result, "status", "empty_request")
                ),
                "stable_prefix_estimated_tokens": getattr(
                    segmentation_result, "stable_prefix_estimated_tokens", None
                ),
                "semi_stable_estimated_tokens": getattr(
                    segmentation_result, "semi_stable_estimated_tokens", None
                ),
                "volatile_estimated_tokens": getattr(
                    segmentation_result, "volatile_estimated_tokens", None
                ),
                "stable_prefix_bytes": getattr(
                    segmentation_result, "stable_prefix_bytes", None
                ),
                "volatile_bytes": getattr(segmentation_result, "volatile_bytes", None),
                "compressible_candidate_count": (
                    segmentation_result.compressible_candidate_count()
                ),
                "protected_count": segmentation_result.protected_count(),
            },
        )
    elif not _segmentation_needed:
        logger.debug(
            "segmentation_skipped",
            extra={
                "proxy_request_id": request_id,
                "model": model_id,
                "protocol": endpoint.protocol,
            },
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
    if (
        lease is not None
        and result.stream_iterator is not None
        and isinstance(response, StreamingResponse)
    ):
        response = StreamingResponse(
            wrap_stream_with_lease(result.stream_iterator, lease),
            status_code=response.status_code,
            media_type=response.media_type,
            headers=response.headers,
        )
    elif lease is not None:
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
        timer.__exit__(*sys.exc_info())
        raise
    timer.__exit__(None, None, None)


def _rewrite_upstream_model(
    payload: dict[str, Any],
    model_id: str,
) -> bytes | None:
    """Forward the normalized, provider-free model ID upstream.

    ``None`` means the original request body can be forwarded byte-for-byte.
    """
    if payload.get("model") == model_id:
        return None
    upstream_payload = dict(payload)
    upstream_payload["model"] = model_id
    return encode_json_body(upstream_payload)
