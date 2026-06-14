"""OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from fastapi.responses import Response, StreamingResponse

from go_aggregator.api.errors import openai_error_response
from go_aggregator.auth import require_auth
from go_aggregator.catalog.protocols import ProtocolMismatchError
from go_aggregator.errors import (
    CatalogUnavailableError,
    ModelNotFoundError,
    NoEligibleAccountError,
    UpstreamExhaustedError,
)
from go_aggregator.request.body import read_body_limited
from go_aggregator.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


async def handle_chat_completions(
    request: Request,
) -> Response:
    """Handle POST /v1/chat/completions."""
    await require_auth(request)

    coordinator: RequestCoordinator = request.app.state.coordinator

    # Enforce body size limit (Section 12.1: bounded chunked reading)
    from go_aggregator.constants import MAX_REQUEST_BODY_BYTES

    try:
        body = await read_body_limited(request, MAX_REQUEST_BODY_BYTES)
    except Exception:
        return openai_error_response(
            status_code=413,
            message="Request body too large",
            error_type="invalid_request_error",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return openai_error_response(
            status_code=400,
            message="Invalid JSON",
            error_type="invalid_request_error",
        )

    model_id = payload.get("model")
    if not model_id:
        return openai_error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )

    is_stream = payload.get("stream", False)

    context = ProxyRequestContext(
        request_id=str(uuid.uuid4()),
        protocol="openai",
        model_id=model_id,
        streaming=is_stream,
        original_body=body,
        incoming_headers=dict(request.headers),
        started_at=time.time(),
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

        return StreamingResponse(
            _stream_gen(),
            status_code=result.status_code,
            headers=result.headers,
            media_type="text/event-stream",
        )

    # Return raw bytes - do not decode and re-serialize
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
        media_type=None,
    )
