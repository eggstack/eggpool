"""Shared request handling for protocol-compatible proxy endpoints."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from fastapi.responses import JSONResponse, Response, StreamingResponse

from eggpool.auth import require_auth
from eggpool.catalog.cache import parse_model_id
from eggpool.catalog.protocols import ProtocolMismatchError
from eggpool.constants import MAX_REQUEST_BODY_BYTES
from eggpool.errors import (
    CatalogUnavailableError,
    ContextLimitExceededError,
    ModelNotFoundError,
    ModelUnavailableError,
    NoEligibleAccountError,
    RequestTooLargeError,
    UpstreamExhaustedError,
)
from eggpool.request.body import read_body_limited
from eggpool.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)

if TYPE_CHECKING:
    from fastapi import Request

    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)

ProtocolName = Literal["openai", "anthropic"]


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


def _estimate_input_tokens(body: bytes) -> int:
    """Estimate input token count from the request body.

    Uses the same conservative heuristic as the coordinator:
    max(1_000, len(body) // 3), capped at 128_000.
    """
    if not body:
        return 1_000
    return min(max(1_000, len(body) // 3), 128_000)


def _extract_output_tokens(payload: dict[str, Any], protocol: str) -> int | None:
    """Extract the requested output token limit from the payload.

    OpenAI-compatible requests may use ``max_tokens`` or
    ``max_completion_tokens``.  Anthropic requests use ``max_tokens``.
    """
    if protocol == "anthropic":
        value = payload.get("max_tokens")
    else:
        value = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def _check_context_limits(
    *,
    model_id: str,
    provider_id: str | None,
    body: bytes,
    payload: dict[str, Any],
    protocol: str,
    catalog_cache: Any,
) -> None:
    """Check request context against effective model limits.

    Raises ``ContextLimitExceededError`` if the estimated request context
    exceeds the configured effective limit for the model.
    """
    model_info = catalog_cache.get_model_for_provider(model_id, provider_id)
    if model_info is None:
        return

    effective = model_info.get("effective_limits")
    if not effective:
        return

    if not effective.get("enforce", True):
        return

    max_context = effective.get("context_tokens")
    max_input = effective.get("input_tokens")
    max_output = effective.get("output_tokens")

    if max_context is None and max_input is None and max_output is None:
        return

    estimated_input = _estimate_input_tokens(body)
    requested_output = _extract_output_tokens(payload, protocol)

    # Check input-specific limit
    if max_input is not None and estimated_input > max_input:
        raise ContextLimitExceededError(
            model_id=model_id,
            estimated_input_tokens=estimated_input,
            requested_output_tokens=requested_output,
            max_context_tokens=max_context,
            max_input_tokens=max_input,
        )

    # Check output-specific limit
    if (
        max_output is not None
        and requested_output is not None
        and requested_output > max_output
    ):
        raise ContextLimitExceededError(
            model_id=model_id,
            estimated_input_tokens=estimated_input,
            requested_output_tokens=requested_output,
            max_context_tokens=max_context,
            max_input_tokens=max_input,
        )

    # Check total context limit
    if max_context is not None:
        total = estimated_input + (requested_output or 0)
        if total > max_context:
            raise ContextLimitExceededError(
                model_id=model_id,
                estimated_input_tokens=estimated_input,
                requested_output_tokens=requested_output,
                max_context_tokens=max_context,
                max_input_tokens=max_input,
            )


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
    if not isinstance(model_value, str) or not model_value:
        return endpoint.error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )

    config = cast("AppConfig | None", getattr(request.app.state, "config", None))
    known_providers = set(config.providers) if config is not None else None
    model_id, provider_id = parse_model_id(model_value, known_providers)

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

    stream_value = payload.get("stream", False)
    if stream_value is not None and not isinstance(stream_value, bool):
        return endpoint.error_response(
            status_code=400,
            message="Invalid stream value: must be a boolean",
            error_type="invalid_request_error",
        )
    is_stream = bool(stream_value)

    context = ProxyRequestContext(
        request_id=str(uuid.uuid4()),
        protocol=endpoint.protocol,
        model_id=model_id,
        streaming=is_stream,
        original_body=body,
        incoming_headers=dict(request.headers),
        started_at=time.time(),
        provider_id=provider_id,
        client_ip=get_client_ip(request),
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
