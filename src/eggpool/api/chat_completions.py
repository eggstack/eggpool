"""OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

from fastapi.responses import Response, StreamingResponse

from eggpool.api.errors import openai_error_response
from eggpool.auth import require_auth
from eggpool.catalog.cache import parse_model_id
from eggpool.catalog.protocols import ProtocolMismatchError
from eggpool.errors import (
    CatalogUnavailableError,
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

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    # Check X-Forwarded-For first (for reverse proxies)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        return forwarded_for.split(",")[0].strip()
    # Check X-Real-IP
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    # Fall back to direct client IP
    if request.client and request.client.host:
        return request.client.host
    return ""


async def handle_chat_completions(
    request: Request,
) -> Response:
    """Handle POST /v1/chat/completions."""
    await require_auth(request)

    coordinator: RequestCoordinator = request.app.state.coordinator

    # Enforce body size limit (Section 12.1: bounded chunked reading)
    from eggpool.constants import MAX_REQUEST_BODY_BYTES

    try:
        body = await read_body_limited(request, MAX_REQUEST_BODY_BYTES)
    except RequestTooLargeError:
        return openai_error_response(
            status_code=413,
            message="Request body too large",
            error_type="invalid_request_error",
        )

    payload_obj: object
    try:
        payload_obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return openai_error_response(
            status_code=400,
            message="Invalid JSON",
            error_type="invalid_request_error",
        )
    if not isinstance(payload_obj, dict):
        return openai_error_response(
            status_code=400,
            message="Invalid JSON",
            error_type="invalid_request_error",
        )
    payload = cast("dict[str, Any]", payload_obj)

    model_value = payload.get("model")
    if not isinstance(model_value, str) or not model_value:
        return openai_error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )
    model_id = model_value

    # Parse provider-suffixed model IDs (e.g. "gpt-4/opencode-go")
    known_providers: set[str] | None = None
    if hasattr(request.app.state, "config"):
        known_providers = set(request.app.state.config.providers)  # type: ignore[union-attr]
    base_model_id, provider_id = parse_model_id(model_id, known_providers)

    stream_value = payload.get("stream", False)
    if stream_value is not None and not isinstance(stream_value, bool):
        return openai_error_response(
            status_code=400,
            message="Invalid stream value: must be a boolean",
            error_type="invalid_request_error",
        )
    is_stream = bool(stream_value)

    context = ProxyRequestContext(
        request_id=str(uuid.uuid4()),
        protocol="openai",
        model_id=base_model_id,
        streaming=is_stream,
        original_body=body,
        incoming_headers=dict(request.headers),
        started_at=time.time(),
        provider_id=provider_id,
        client_ip=_get_client_ip(request),
    )

    logger.info(
        "Proxying chat completion: model=%s proxy_request_id=%s streaming=%s",
        model_id,
        context.request_id,
        is_stream,
    )

    try:
        result = await coordinator.execute(context)
    except ModelNotFoundError as exc:
        return openai_error_response(
            status_code=404,
            message=str(exc),
            error_type="invalid_request_error",
        )
    except NoEligibleAccountError as exc:
        return openai_error_response(
            status_code=503,
            message=str(exc),
            error_type="server_error",
        )
    except CatalogUnavailableError as exc:
        return openai_error_response(
            status_code=503,
            message=str(exc),
            error_type="server_error",
        )
    except ModelUnavailableError as exc:
        return openai_error_response(
            status_code=503,
            message=str(exc),
            error_type="server_error",
        )
    except UpstreamExhaustedError as exc:
        return openai_error_response(
            status_code=502,
            message=str(exc),
            error_type="server_error",
        )
    except ProtocolMismatchError as exc:
        return openai_error_response(
            status_code=400,
            message=str(exc),
            error_type="invalid_request_error",
        )

    return _render_response(result)


def _render_response(
    result: PreparedProxyResponse,
) -> Response:
    """Render a PreparedProxyResponse as a FastAPI response.

    For non-streaming responses, returns raw bytes to preserve upstream
    content-type and body exactly. For streaming, uses StreamingResponse.
    """
    if result.stream_iterator is not None:
        stream_iter = result.stream_iterator

        async def _stream_gen():  # type: ignore[no-untyped-def]
            async for chunk in stream_iter:
                yield chunk

        resp: Response = StreamingResponse(
            _stream_gen(),
            status_code=result.status_code,
            media_type=None,
        )
        for name, value in result.headers:
            resp.headers.append(name, value)
        return resp

    # Return raw bytes - do not decode and re-serialize
    resp = Response(
        content=result.body,
        status_code=result.status_code,
        media_type=None,
    )
    for name, value in result.headers:
        resp.headers.append(name, value)
    return resp
