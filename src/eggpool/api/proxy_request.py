"""Shared request handling for protocol-compatible proxy endpoints."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi.responses import JSONResponse, Response, StreamingResponse

from eggpool.api.errors import (
    anthropic_capability_error_response,
    openai_capability_error_response,
)
from eggpool.auth import require_auth
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
from eggpool.request.body import encode_json_body, read_body_limited
from eggpool.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.request.limits import (
    ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR,
)
from eggpool.request.limits import (
    check_context_limits as _check_context_limits,
)
from eggpool.routing.provider import parse_model_provider
from eggpool.transcoder.context import TranscodeContext

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
        total_bytes += len(json.dumps(tool))
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
    translated, warnings = transcoder.encode_request(
        payload, transcode_context, features=_features
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
    await require_auth(request)

    coordinator = cast("RequestCoordinator", request.app.state.coordinator)
    try:
        body = await read_body_limited(request, MAX_REQUEST_BODY_BYTES)
    except RequestTooLargeError:
        return endpoint.error_response(
            status_code=413,
            message="Request body too large",
            error_type="invalid_request_error",
        )

    payload_obj: object
    try:
        payload_obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
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
    model_id, provider_id = parse_model_provider(model_value, known_providers)

    # Preflight context limit check (guardrail, not primary enforcement).
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is not None:
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
        transcoder_policy = getattr(request.app.state, "transcoder_policy", None)
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
                translated_body = json.dumps(preflight.translated_payload).encode()
                if preflight.tool_token_padding > 0:
                    translated_body += b"\x00" * (
                        preflight.tool_token_padding
                        * ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR
                    )
                _check_context_limits(
                    model_id=model_id,
                    provider_id=provider_id,
                    body=translated_body,
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

    # Phase 2: run the canonical request segmenter.  The result is
    # attached to the ProxyRequestContext so the finalizer can persist
    # the segmentation summary, the deterministic stable_prefix_hash /
    # request_shape_hash, and the segment-kind token / byte
    # estimates.  The segmenter is observational: it never mutates the
    # payload, never raises on malformed input, and is cheap enough
    # to run on every request without blocking the request path.
    segmentation_result: Any = None
    try:
        from eggpool.transcoder.segmentation import segment_request

        segmentation_result = segment_request(payload, protocol=endpoint.protocol)
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

    context = ProxyRequestContext(
        request_id=request_id,
        protocol=endpoint.protocol,
        model_id=model_id,
        streaming=is_stream,
        original_body=body,
        incoming_headers=dict(request.headers),
        started_at=time.time(),
        provider_id=provider_id,
        client_ip=get_client_ip(request),
        upstream_body=_rewrite_upstream_model(payload, model_id),
        upstream_protocol=endpoint.protocol,
        transcode_required=False,
        transcode_context=transcode_ctx,
        segmentation=segmentation_result,
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

    logger.info(
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

    return render_proxy_response(result)


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
